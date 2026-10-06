"""Structural transport of an already owned, frozen Instagram source cart.

No current-context reads, shipping inference, price authority or ORM live here.
Absence is historical unknown; a present malformed binding always fails closed.
"""
from decimal import Decimal
from functools import wraps
import json
from uuid import UUID

from management.services import ig_revision_cart_binding as binding_contract

SCHEMA = "ig-checkout-cart-provenance.v1"
MAX_EXPANDED_ROWS = 64
OWNER_KEYS = {"proposal_id", "proposal_pk", "revision_id", "revision", "client_id", "deal_id", "episode_id"}
BINDING_KEYS = {"schema", "scope", "source_capture_digest", "session_id", "generation", "selection_revision",
    "active_line_id", "active_index", "checkout_item_limit", "lines", "source_binding_digest",
    "quote_line_map", "quote_binding_digest"}


class CartProvenanceError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _reject(reason):
    raise CartProvenanceError(reason)


def _finite(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except CartProvenanceError:
            raise
        except (KeyError, IndexError, TypeError, ValueError, AttributeError, OverflowError, RecursionError) as exc:
            raise CartProvenanceError("cart_provenance_invalid") from exc
    return guarded


def _copy(value):
    try:
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode()) > 1024 * 1024:
            _reject("cart_provenance_too_large")
        return json.loads(encoded)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        if isinstance(exc, CartProvenanceError):
            raise
        raise CartProvenanceError("cart_provenance_invalid") from exc


def _positive(value):
    return type(value) is int and 0 < value <= (1 << 63) - 1


def _money(value):
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0:
            _reject("cart_provenance_totals_invalid")
        return number
    except (ValueError, ArithmeticError) as exc:
        raise CartProvenanceError("cart_provenance_totals_invalid") from exc


def _owner(owner):
    if not isinstance(owner, dict) or set(owner) != OWNER_KEYS:
        _reject("cart_provenance_owner_invalid")
    if any(not _positive(owner[key]) for key in OWNER_KEYS - {"proposal_id"}):
        _reject("cart_provenance_owner_invalid")
    try:
        if str(UUID(owner["proposal_id"])) != owner["proposal_id"]:
            _reject("cart_provenance_owner_invalid")
    except (ValueError, TypeError, AttributeError) as exc:
        raise CartProvenanceError("cart_provenance_owner_invalid") from exc
    return _copy(owner)


def _binding(raw, owner):
    binding = _copy(raw)
    if not isinstance(binding, dict) or set(binding) != BINDING_KEYS:
        _reject("cart_source_binding_invalid")
    try:
        valid = binding_contract._source_digest_matches(binding) and binding_contract._quote_digest_matches(binding)
    except (KeyError, TypeError, ValueError, RecursionError) as exc:
        raise CartProvenanceError("cart_source_binding_invalid") from exc
    if not valid:
        _reject("cart_source_binding_digest_invalid")
    scope = binding.get("scope")
    if (not isinstance(scope, dict) or set(scope) != set(binding_contract.SCOPE_KEYS)
            or binding_contract._scope_reason(scope, scope)
            or scope["client_id"] != owner["client_id"] or scope["episode_id"] != owner["episode_id"]):
        _reject("cart_source_binding_owner_changed")
    if binding["checkout_item_limit"] not in {8, 12} or len(binding["lines"]) > binding["checkout_item_limit"]:
        _reject("cart_source_binding_item_limit")
    if any(not _positive(binding[key]) for key in ("session_id", "generation", "selection_revision")):
        _reject("cart_source_binding_head_invalid")
    seen = set()
    for index, line in enumerate(binding["lines"]):
        if (not isinstance(line, dict) or set(line) != {"line_id", "recipient_id", "index", "choices", "evidence", "quantity_default", "source_selection"}
                or not binding_contract._identity(line.get("line_id")) or not binding_contract._identity(line.get("recipient_id"))
                or line["line_id"] in seen or type(line["index"]) is not int or line["index"] != index):
            _reject("cart_source_binding_line_invalid")
        seen.add(line["line_id"])
        selection = line["source_selection"]
        expected = {"client_id": owner["client_id"], "episode_id": owner["episode_id"],
            "session_id": binding["session_id"], "generation": binding["generation"],
            "revision": binding["selection_revision"], "reset_floor": scope["reset_floor"],
            "line_id": line["line_id"], "recipient_id": line["recipient_id"]}
        line_scope = selection.get("scope") if isinstance(selection, dict) else None
        if (not isinstance(selection, dict) or selection.get("schema") != "source-selection.v1"
                or not isinstance(line_scope, dict) or any(
                    line_scope.get(key) != value or type(line_scope.get(key)) is not type(value)
                    for key, value in expected.items())):
            _reject("cart_source_binding_line_owner_changed")
    active = binding["active_index"]
    if type(active) is not int or not 0 <= active < len(binding["lines"]) or binding["lines"][active]["line_id"] != binding["active_line_id"]:
        _reject("cart_source_binding_head_invalid")
    return binding


def _configuration(raw):
    configured = binding_contract._configuration(raw)
    if configured is None:
        _reject("cart_provenance_configuration_invalid")
    return configured


def legacy_unknown():
    return {"schema": SCHEMA, "status": "legacy_unknown", "reason": "source_cart_never_captured"}


@_finite
def capture_proposal_provenance(*, revision_snapshot, owner, proposal_items):
    """Validate the original ordered proposal before catalog/provider effects.

    proposal_items is a plain server projection of persisted proposal rows.
    Presence is tested separately from truthiness; null/empty is not legacy.
    """
    if not isinstance(revision_snapshot, dict):
        _reject("cart_revision_snapshot_invalid")
    if "source_cart_binding" not in revision_snapshot:
        return legacy_unknown()
    owner = _owner(owner)
    binding = _binding(revision_snapshot["source_cart_binding"], owner)
    frozen = revision_snapshot.get("items")
    maps = binding["quote_line_map"]
    if (not isinstance(proposal_items, (list, tuple)) or not isinstance(frozen, list)
            or len(proposal_items) != len(maps) or len(frozen) != len(maps)):
        _reject("cart_proposal_line_count_changed")
    groups, ids = [], set()
    for index, item in enumerate(proposal_items):
        if not isinstance(item, dict) or not _positive(item.get("proposal_item_id")) or item["proposal_item_id"] in ids:
            _reject("cart_proposal_item_identity_invalid")
        if type(item.get("position")) is not int or item["position"] != index:
            _reject("cart_proposal_item_order_changed")
        ids.add(item["proposal_item_id"])
        config = _configuration(item)
        if config != maps[index]["configuration"] or _configuration(frozen[index]) != config:
            _reject("cart_proposal_configuration_changed")
        if (_money(frozen[index].get("line_total")) != _money(item.get("line_total"))
                or _money(frozen[index].get("unit_price")) != _money(item.get("unit_price"))):
            _reject("cart_proposal_totals_changed")
        groups.append({"proposal_item_id": item["proposal_item_id"], "quote_position": index,
            "configuration": config, "line_total": str(_money(item.get("line_total"))),
            "unit_price": str(_money(item.get("unit_price")))})
    return {"schema": SCHEMA, "status": "bound", "owner": owner, "binding": binding,
        "proposal_items": groups, "item_map": []}


def _expanded_map(provenance, cart_items):
    if not isinstance(cart_items, (list, tuple)) or not 0 < len(cart_items) <= MAX_EXPANDED_ROWS:
        _reject("cart_expansion_invalid")
    groups = provenance["proposal_items"]
    by_id = {row["proposal_item_id"]: row for row in groups}
    totals = {identity: [0, Decimal("0")] for identity in by_id}
    rows, previous = [], -1
    for position, item in enumerate(cart_items):
        if not isinstance(item, dict) or type(item.get("proposal_item_id")) is not int:
            _reject("cart_expansion_item_identity_invalid")
        group = by_id.get(item["proposal_item_id"])
        if group is None:
            _reject("cart_expansion_item_identity_invalid")
        quote_position = group["quote_position"]
        if quote_position < previous:
            _reject("cart_expansion_order_changed")
        previous = quote_position
        config = _configuration(item)
        if {**config, "qty": group["configuration"]["qty"]} != group["configuration"]:
            _reject("cart_expansion_configuration_changed")
        amount, unit = _money(item.get("line_total")), _money(item.get("unit_price"))
        if unit * config["qty"] != amount:
            _reject("cart_expansion_totals_changed")
        totals[group["proposal_item_id"]][0] += config["qty"]
        totals[group["proposal_item_id"]][1] += amount
        source = provenance["binding"]["quote_line_map"][quote_position]
        rows.append({"cart_position": position, "proposal_item_id": group["proposal_item_id"],
            "quote_position": quote_position, "line_id": source["line_id"], "recipient_id": source["recipient_id"],
            "configuration": config})
    for group in groups:
        quantity, total = totals[group["proposal_item_id"]]
        if quantity != group["configuration"]["qty"] or total != _money(group["line_total"]):
            _reject("cart_expansion_group_changed")
    return rows


@_finite
def bind_expanded_cart(provenance, *, cart_items):
    if provenance == legacy_unknown():
        return legacy_unknown()
    result = _copy(provenance)
    result["item_map"] = _expanded_map(result, cart_items)
    return result


@_finite
def validate_attempt_provenance(snapshot):
    """Use only original attempt-owned scope, including after a later reset."""
    if not isinstance(snapshot, dict):
        _reject("cart_attempt_snapshot_invalid")
    if "source_cart_provenance" not in snapshot:
        if "source_cart_binding" in snapshot:
            _reject("cart_attempt_provenance_missing")
        return legacy_unknown()
    raw = snapshot["source_cart_provenance"]
    if raw == legacy_unknown():
        if "source_cart_binding" in snapshot:
            _reject("cart_attempt_provenance_missing")
        return legacy_unknown()
    result = _copy(raw)
    if not isinstance(result, dict) or set(result) != {"schema", "status", "owner", "binding", "proposal_items", "item_map"}:
        _reject("cart_attempt_provenance_invalid")
    if result["schema"] != SCHEMA or result["status"] != "bound":
        _reject("cart_attempt_provenance_invalid")
    owner = _owner(result["owner"])
    if snapshot.get("proposal_id") != owner["proposal_id"] or snapshot.get("checkout_surface") != "instagram_proposal":
        _reject("cart_attempt_owner_changed")
    binding = _binding(result["binding"], owner)
    if "source_cart_binding" in snapshot and _copy(snapshot["source_cart_binding"]) != binding:
        _reject("cart_attempt_binding_changed")
    groups = result["proposal_items"]
    if not isinstance(groups, list) or len(groups) != len(binding["quote_line_map"]):
        _reject("cart_attempt_proposal_items_invalid")
    ids = set()
    for index, group in enumerate(groups):
        if (not isinstance(group, dict) or set(group) != {"proposal_item_id", "quote_position", "configuration", "line_total", "unit_price"}
                or not _positive(group["proposal_item_id"]) or group["proposal_item_id"] in ids
                or type(group["quote_position"]) is not int or group["quote_position"] != index
                or group["configuration"] != binding["quote_line_map"][index]["configuration"]):
            _reject("cart_attempt_proposal_items_invalid")
        ids.add(group["proposal_item_id"])
        _money(group["line_total"]); _money(group["unit_price"])
    if result["item_map"] != _expanded_map(result, snapshot.get("cart")):
        _reject("cart_attempt_item_map_changed")
    return result


@_finite
def bind_order_items(provenance, *, cart_items, order_id, order_items):
    """Attach exact bulk-created IDs in expanded-cart order, never source-line zip."""
    if provenance == legacy_unknown():
        return legacy_unknown()
    if not _positive(order_id) or not isinstance(order_items, (list, tuple)) or len(order_items) != len(cart_items):
        _reject("cart_order_items_unavailable")
    result = _copy(provenance)
    if result["item_map"] != _expanded_map(result, cart_items):
        _reject("cart_attempt_item_map_changed")
    seen = set()
    for position, row in enumerate(order_items):
        if (not isinstance(row, dict) or not _positive(row.get("id")) or row["id"] in seen
                or row.get("order_id") != order_id or _configuration(row) != result["item_map"][position]["configuration"]
                or _money(row.get("line_total")) != _money(cart_items[position].get("line_total"))
                or _money(row.get("unit_price")) != _money(cart_items[position].get("unit_price"))):
            _reject("cart_order_items_changed")
        seen.add(row["id"])
        result["item_map"][position]["order_item_id"] = row["id"]
    result["order_id"] = order_id
    return result
