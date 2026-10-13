"""Pure bridge from an owned source-cart capture to a frozen checkout artifact.

Inputs must come from the canonical source/catalog readers, never model JSON.
Structural validation here does not authenticate a dict or authorize a payment.
The effect owner must re-capture under its existing source/permission/CAS fence.
No ORM, money calculation, shipping inference, or provider calls live here.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import wraps
import hashlib
import json
import re

SCHEMA = "ig-checkout-source-cart.v1"
ASSISTED_SCHEMA = "ig-checkout-source-cart.v2"
MAX_ASSISTED_BINDING_BYTES = 1024 * 1024
SOURCE_SCHEMA = "source-selections.v1"
MAX_SOURCE_LINES = 16
LIVE_CHECKOUT_ITEM_LIMIT = 8
CATALOG_CHECKOUT_ITEM_LIMIT = 12
SCOPE_KEYS = ("client_id", "episode_id", "order_id", "reset_id", "reset_floor", "source_namespace")
CHOICE_KEYS = ("product_id", "size", "fit_option_code", "color", "quantity", "color_variant_id", "option_values")
SOURCE_AUTHORITIES = frozenset({"customer_source", "audited_correction", "validated_selection_action"})


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _copy(value):
    return json.loads(_json(value))


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _positive(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _hash(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _identity(value):
    return isinstance(value, str) and 0 < len(value) <= 128 and not any(ord(c) < 32 for c in value)


@dataclass(frozen=True)
class CartBindingResult:
    status: str
    reason: str = ""
    missing: tuple[str, ...] = ()
    _payload_json: str = "{}"

    @property
    def binding(self):
        """A detached JSON payload; changing it cannot mutate this capture."""
        return json.loads(self._payload_json)

    @property
    def ok(self):
        return self.status in {"captured", "ready"}

    def as_dict(self):
        return {"status": self.status, "reason": self.reason,
                "missing": list(self.missing), "binding": self.binding}


def _result(status, reason="", *, payload=None, missing=()):
    return CartBindingResult(status, reason, tuple(missing), _json(payload or {}))


def _finite_input(function):
    @wraps(function)
    def bounded(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (TypeError, ValueError, OverflowError, RecursionError):
            return _result("unavailable", "cart_capture_invalid")
    return bounded


def _scope_reason(scope, expected):
    if not isinstance(scope, dict) or not isinstance(expected, dict):
        return "cart_scope_unknown"
    if any(key not in scope or key not in expected for key in SCOPE_KEYS):
        return "cart_scope_unknown"
    if not _positive(expected.get("client_id")) or not _positive(expected.get("reset_floor")):
        return "cart_scope_unknown"
    if not _identity(expected.get("source_namespace")):
        return "cart_scope_unknown"
    for key in ("episode_id", "order_id", "reset_id"):
        if expected[key] is not None and not _positive(expected[key]):
            return "cart_scope_unknown"
    if any(scope[key] != expected[key] or type(scope[key]) is not type(expected[key]) for key in SCOPE_KEYS):
        return "cart_scope_changed"
    return ""


def _proof_reason(proof, reset_floor):
    if not isinstance(proof, dict) or not _positive(proof.get("source_message_id")) or not _hash(proof.get("source_digest")):
        return "cart_source_proof_missing"
    if proof["source_message_id"] < reset_floor:
        return "cart_source_before_reset"
    if not _positive(proof.get("transition_id")):
        return "cart_source_proof_missing"
    # Copy operations retain the original proof and their current intent proof.
    copy_intent = proof.get("copy_intent")
    if copy_intent is not None:
        if not isinstance(copy_intent, dict) or not _positive(copy_intent.get("source_message_id")) or not _hash(copy_intent.get("source_digest")):
            return "cart_copy_proof_missing"
        if copy_intent["source_message_id"] < reset_floor:
            return "cart_source_before_reset"
    return ""


@_finite_input
def build_cart_binding(*, captured_cart, expected_scope, checkout_item_limit=LIVE_CHECKOUT_ITEM_LIMIT):
    """Capture every current line, including unresolved ones; never truncate.

    Quantity 1 is usable only from the existing canonical default envelope. It
    remains explicitly unconfirmed. This result contains no price authority.
    """
    if checkout_item_limit not in {LIVE_CHECKOUT_ITEM_LIMIT, CATALOG_CHECKOUT_ITEM_LIMIT}:
        return _result("unavailable", "cart_limit_unknown")
    if not isinstance(captured_cart, dict) or captured_cart.get("schema") != SOURCE_SCHEMA:
        return _result("unavailable", "cart_capture_missing")
    if captured_cart.get("status") != "captured":
        return _result("unavailable", "cart_capture_unavailable")
    reason = _scope_reason(captured_cart.get("scope"), expected_scope)
    if reason:
        return _result("conflict", reason)
    if not _hash(captured_cart.get("capture_digest")):
        return _result("unavailable", "cart_capture_digest_missing")
    for key in ("session_id", "generation", "selection_revision"):
        if not _positive(captured_cart.get(key)):
            return _result("unavailable", "cart_head_unknown")
    rows = captured_cart.get("lines")
    if not isinstance(rows, list) or not rows:
        return _result("partial", "cart_empty")
    if len(rows) > MAX_SOURCE_LINES or len(rows) > checkout_item_limit:
        return _result("partial", "cart_item_limit")
    lines, seen, missing = [], set(), []
    for position, row in enumerate(rows):
        if not isinstance(row, dict) or not _identity(row.get("line_id")) or not _identity(row.get("recipient_id")):
            return _result("conflict", "cart_line_scope_unknown")
        line_id, recipient_id = row["line_id"], row["recipient_id"]
        if line_id in seen or row.get("index") != position or isinstance(row.get("index"), bool):
            return _result("conflict", "cart_line_identity_invalid")
        seen.add(line_id)
        selection = row.get("source_selection")
        if not isinstance(selection, dict) or selection.get("schema") != "source-selection.v1":
            return _result("unavailable", "cart_line_capture_missing")
        line_scope = selection.get("scope")
        if not isinstance(line_scope, dict):
            return _result("conflict", "cart_line_scope_unknown")
        own = {"client_id": expected_scope["client_id"], "episode_id": expected_scope["episode_id"],
               "line_id": line_id, "recipient_id": recipient_id,
               "session_id": captured_cart["session_id"], "generation": captured_cart["generation"],
               "revision": captured_cart["selection_revision"], "reset_floor": expected_scope["reset_floor"]}
        if any(key not in line_scope or line_scope[key] != value or type(line_scope[key]) is not type(value) for key, value in own.items()):
            return _result("conflict", "cart_line_scope_changed")
        # Original source-selection.v1 may lack order/namespace; its exact
        # parent capture owns those bindings. Explicit foreign values fail.
        if any(key in line_scope and line_scope[key] != expected_scope[key] for key in SCOPE_KEYS):
            return _result("conflict", "cart_line_scope_changed")
        fields = selection.get("fields")
        if not isinstance(fields, dict):
            return _result("unavailable", "cart_line_fields_missing")
        choices, proofs = {}, {}
        for key in CHOICE_KEYS:
            field = fields.get(key)
            if field is None or (isinstance(field, dict) and field.get("status") in {"unknown", "ambiguous", "not_applicable"}):
                continue
            if not isinstance(field, dict) or field.get("status") != "confirmed" or field.get("authority") not in SOURCE_AUTHORITIES:
                return _result("conflict", "cart_choice_authority_invalid")
            reason = _proof_reason(field.get("source"), expected_scope["reset_floor"])
            if reason:
                return _result("conflict", reason)
            choices[key], proofs[key] = _copy(field.get("value")), _copy(field["source"])
        if not _positive(choices.get("product_id")):
            missing.append(line_id + ":product")
        if "quantity" in choices and not _positive(choices["quantity"]):
            return _result("conflict", "cart_quantity_invalid")
        quantity_default = None
        if "quantity" not in choices:
            default = (row.get("defaults") or {}).get("quantity") if isinstance(row.get("defaults", {}), dict) else None
            if (isinstance(default, dict) and type(default.get("value")) is int and default["value"] == 1
                    and default.get("authority") == "existing_cart_default" and default.get("source_confirmed") is False):
                quantity_default = _copy(default)
            else:
                missing.append(line_id + ":quantity")
        lines.append({"line_id": line_id, "recipient_id": recipient_id, "index": position,
                      "choices": choices, "evidence": proofs, "quantity_default": quantity_default,
                      "source_selection": _copy(selection)})
    if captured_cart.get("active_line_id") not in seen or not isinstance(captured_cart.get("active_index"), int) or isinstance(captured_cart.get("active_index"), bool):
        return _result("conflict", "cart_active_line_invalid")
    active_index = captured_cart["active_index"]
    if not 0 <= active_index < len(lines) or lines[active_index]["line_id"] != captured_cart["active_line_id"]:
        return _result("conflict", "cart_active_line_invalid")
    if expected_scope["episode_id"] is None:
        missing.append("scope:episode")
    payload = {"schema": SCHEMA, "scope": _copy(expected_scope),
               "source_capture_digest": captured_cart["capture_digest"],
               "session_id": captured_cart["session_id"], "generation": captured_cart["generation"],
               "selection_revision": captured_cart["selection_revision"],
               "active_line_id": captured_cart["active_line_id"], "active_index": active_index,
               "checkout_item_limit": checkout_item_limit, "lines": lines}
    payload["source_binding_digest"] = _digest(payload)
    return _result("partial" if missing else "captured", "cart_selection_incomplete" if missing else "",
                   payload=payload, missing=missing)


def _binding_payload(binding):
    if isinstance(binding, CartBindingResult):
        return binding.binding if binding.ok else None
    if not isinstance(binding, dict) or binding.get("schema") != SCHEMA:
        return None
    return _copy(binding)


def _source_digest_matches(payload):
    if (not isinstance(payload, dict) or payload.get("schema") != SCHEMA
            or not isinstance(payload.get("lines"), list) or not payload["lines"]
            or len(payload["lines"]) > MAX_SOURCE_LINES):
        return False
    source = {key: value for key, value in payload.items() if key not in {"source_binding_digest", "quote_line_map", "quote_binding_digest"}}
    return _hash(payload.get("source_binding_digest")) and _digest(source) == payload["source_binding_digest"]


def _quote_digest_matches(payload):
    rows = payload.get("quote_line_map")
    if not isinstance(rows, list) or len(rows) != len(payload.get("lines", [])):
        return False
    for index, (row, line) in enumerate(zip(rows, payload["lines"])):
        if (not isinstance(row, dict) or _configuration(row.get("configuration")) is None
                or row.get("line_id") != line["line_id"]
                or row.get("recipient_id") != line["recipient_id"] or row.get("quote_position") != index
                or _configuration(row.get("configuration")) != row.get("configuration")
                or row.get("configuration_digest") != _digest(row.get("configuration"))):
            return False
    return payload.get("quote_binding_digest") == _digest({
        "source_binding_digest": payload.get("source_binding_digest"), "quote_line_map": rows})


@_finite_input
def validate_cart_binding(*, binding, current_capture, expected_scope):
    """Compare source/head scope to a newly owned capture.

    A stored artifact is not current proof. This does not refresh catalog
    prices/readiness or replace revision, permission, invoice/window guards.
    """
    payload = _binding_payload(binding)
    if not _source_digest_matches(payload):
        return _result("conflict", "cart_binding_digest_invalid")
    if "quote_line_map" in payload and not _quote_digest_matches(payload):
        return _result("conflict", "cart_quote_binding_digest_invalid")
    fresh = build_cart_binding(captured_cart=current_capture, expected_scope=expected_scope,
                              checkout_item_limit=payload.get("checkout_item_limit"))
    if not fresh.ok:
        return fresh
    if fresh.binding["source_binding_digest"] != payload["source_binding_digest"]:
        return _result("conflict", "cart_selection_changed")
    return _result("ready" if "quote_line_map" in payload else "captured", payload=payload)


def _configuration(item):
    if not isinstance(item, dict):
        return None
    product_id, quantity = item.get("product_id"), item.get("qty", item.get("quantity"))
    variant = item.get("color_variant_id")
    if not _positive(product_id) or not _positive(quantity) or (variant is not None and not _positive(variant)):
        return None
    size, fit, options = item.get("size", ""), item.get("fit_option_code", item.get("fit_code", "")), item.get("option_values", {})
    if not isinstance(size, str) or not isinstance(fit, str) or not isinstance(options, dict):
        return None
    if any(not isinstance(k, str) or not isinstance(v, str) for k, v in options.items()):
        return None
    return {"product_id": product_id, "qty": quantity, "color_variant_id": variant,
            "size": size.strip().upper(), "fit_option_code": fit.strip().lower(),
            "option_values": {k.strip().lower(): v.strip().lower() for k, v in options.items()}}


def union_configuration(item, *, schema=SCHEMA):
    """Strict versioned transport; structure alone never grants authority."""
    if schema == SCHEMA:
        return _configuration(item)
    if schema != ASSISTED_SCHEMA or not isinstance(item, dict):
        return None
    if item.get('product_id') is not None:
        if item.get('is_custom') is True:
            return None
        return _configuration(item)
    proof = item.get('assisted_line')
    if (item.get('is_custom') is not True or item.get('color_variant_id') is not None
        or not isinstance(proof, dict) or set(proof) != {
            'line_id', 'recipient_id', 'original_index', 'decision_id', 'decision_digest', 'line_digest'}
        or not _identity(proof['line_id']) or not _identity(proof['recipient_id'])
        or type(proof['original_index']) is not int or proof['original_index'] < 0
        or not _hash(proof['decision_digest']) or not _hash(proof['line_digest'])):
        return None
    from uuid import UUID
    try:
        if str(UUID(proof['decision_id'])) != proof['decision_id']:
            return None
    except (ValueError, TypeError, AttributeError):
        return None
    quantity = item.get('qty', item.get('quantity'))
    if type(quantity) is not int or not 1 <= quantity <= 50 or item.get('currency') != 'UAH':
        return None
    values = {}
    for key, limit, required in (
        ('title', 200, True), ('color_name_custom', 100, True), ('size', 16, True),
        ('fit_option_code', 50, False), ('fit_option_label', 100, False),
    ):
        value = item.get(key, '')
        if (not isinstance(value, str) or len(value) > limit
            or any(ord(char) < 32 for char in value) or (required and not value.strip())):
            return None
        values[key] = value
    if item.get('option_values', {}) != {} or item.get('option_labels', {}) != {}:
        return None
    image, image_digest = item.get('photo_asset_id'), item.get('photo_digest', '')
    try:
        if image is not None and (str(UUID(image)) != image or not _hash(image_digest)):
            return None
        if image is None and image_digest:
            return None
    except (ValueError, TypeError, AttributeError):
        return None
    return {'item_kind': 'custom_apparel', 'product_id': None, 'color_variant_id': None,
        'is_custom': True, 'qty': quantity, 'currency': 'UAH', **values,
        'option_values': {}, 'option_labels': {}, 'photo_asset_id': image,
        'photo_digest': image_digest, 'assisted_line': _copy(proof)}


def _assisted_source_digest_matches(payload):
    if not isinstance(payload, dict) or payload.get('schema') != ASSISTED_SCHEMA:
        return False
    source = {key: value for key, value in payload.items()
        if key not in {'source_binding_digest', 'quote_line_map', 'quote_binding_digest'}}
    return _hash(payload.get('source_binding_digest')) and _digest(source) == payload['source_binding_digest']


def _assisted_quote_digest_matches(payload):
    rows, lines = payload.get('quote_line_map'), payload.get('lines')
    if not isinstance(rows, list) or not isinstance(lines, list) or len(rows) != len(lines):
        return False
    for index, (row, line) in enumerate(zip(rows, lines)):
        if (not isinstance(row, dict) or set(row) != {'line_id', 'recipient_id', 'quote_position',
                'configuration', 'configuration_digest', 'quantity_source_confirmed'}
            or row['line_id'] != line['line_id'] or row['recipient_id'] != line['recipient_id']
            or type(row['quote_position']) is not int or row['quote_position'] != index
            or row['quantity_source_confirmed'] is not True
            or union_configuration(row['configuration'], schema=ASSISTED_SCHEMA) != row['configuration']
            or row['configuration_digest'] != _digest(row['configuration'])):
            return False
    return payload.get('quote_binding_digest') == _digest({
        'source_binding_digest': payload.get('source_binding_digest'), 'quote_line_map': rows})


def _finite_assisted_input(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, OverflowError, RecursionError):
            return _result('conflict', 'assisted_cart_binding_invalid')
    return guarded


@_finite_assisted_input
def validate_assisted_cart_binding(binding, *, episode_id=None):
    """Validate frozen full READY positional parity, not manager/purchase truth."""
    # The catalogue v1 parser deliberately rejects every other schema. The
    # assisted branch has its own strict extraction, never a v1 fallback.
    if isinstance(binding, CartBindingResult):
        binding = binding.binding if binding.ok else None
    payload = None
    if isinstance(binding, dict) and binding.get('schema') == ASSISTED_SCHEMA:
        encoded = _json(binding)
        if len(encoded.encode()) > MAX_ASSISTED_BINDING_BYTES:
            return _result('conflict', 'assisted_cart_binding_bound')
        payload = json.loads(encoded)
    expected = {'schema', 'scope', 'bound_episode_id', 'original_full_capture', 'current_capture',
        'assisted_commerce', 'source_capture_digest', 'checkout_item_limit', 'lines',
        'source_binding_digest', 'quote_line_map', 'quote_binding_digest'}
    if (not isinstance(payload, dict) or set(payload) != expected
        or not _assisted_source_digest_matches(payload) or not _assisted_quote_digest_matches(payload)):
        return _result('conflict', 'assisted_cart_binding_invalid')
    scope, original, current, evidence = (payload['scope'], payload['original_full_capture'],
        payload['current_capture'], payload['assisted_commerce'])
    evidence_keys = {'schema', 'request_id', 'decision_id', 'client_id', 'intention_id',
        'request_version', 'decision_version', 'request_digest', 'decision_digest',
        'full_capture_digest', 'snapshot_digest', 'expires_at', 'currency', 'line_digests'}
    if (_scope_reason(scope, scope) or not isinstance(original, dict) or not isinstance(current, dict)
        or stable_checkout_source_capture(original) is None or stable_checkout_source_capture(current) is None
        or original.get('scope') != scope or original.get('capture_digest') != payload['source_capture_digest']
        or not isinstance(evidence, dict) or set(evidence) != evidence_keys
        or evidence.get('schema') != 'ig-assisted-quote.v1'
        or type(evidence.get('client_id')) is not int or evidence['client_id'] != scope['client_id']
        or not _positive(evidence.get('request_version')) or not _positive(evidence.get('decision_version'))
        or any(not _hash(evidence.get(key)) for key in (
            'request_digest', 'decision_digest', 'full_capture_digest', 'snapshot_digest'))
        or evidence.get('currency') != 'UAH'
        or evidence.get('full_capture_digest') != original.get('capture_digest')
        or payload['checkout_item_limit'] != CATALOG_CHECKOUT_ITEM_LIMIT
        or not isinstance(payload['lines'], list) or not 1 <= len(payload['lines']) <= CATALOG_CHECKOUT_ITEM_LIMIT):
        return _result('conflict', 'assisted_cart_owner_changed')
    from datetime import datetime
    from uuid import UUID
    if any(str(UUID(evidence[key])) != evidence[key] for key in ('request_id', 'decision_id', 'intention_id')):
        return _result('conflict', 'assisted_cart_owner_changed')
    expiry = datetime.fromisoformat(evidence['expires_at'])
    if expiry.tzinfo is None or not isinstance(evidence['line_digests'], list) or (
        len(evidence['line_digests']) != len(payload['lines'])
        or any(not _hash(value) for value in evidence['line_digests'])):
        return _result('conflict', 'assisted_cart_approval_changed')
    bound = payload['bound_episode_id']
    if bound is not None and not _positive(bound):
        return _result('conflict', 'assisted_cart_owner_changed')
    if scope['episode_id'] is not None and bound != scope['episode_id']:
        return _result('conflict', 'assisted_cart_owner_changed')
    if episode_id is not None and (type(episode_id) is not int or bound != episode_id):
        return _result('conflict', 'assisted_cart_owner_changed')
    originals, currents = original.get('lines'), current.get('lines')
    if (not isinstance(originals, list) or not isinstance(currents, list)
        or len(originals) != len(payload['lines']) or len(currents) != len(originals)):
        return _result('conflict', 'assisted_cart_line_coverage_changed')
    identities = set()
    for index, (line, source, fresh, quote) in enumerate(zip(payload['lines'], originals,
            currents, payload['quote_line_map'])):
        if (not isinstance(line, dict) or set(line) != {'line_id', 'recipient_id', 'index', 'source_selection'}
            or not _identity(line['line_id']) or not _identity(line['recipient_id'])
            or line['line_id'] in identities or type(line['index']) is not int or line['index'] != index
            or source.get('line_id') != line['line_id'] or source.get('recipient_id') != line['recipient_id']
            or source.get('index') != index or line['source_selection'] != source.get('source_selection')
            or fresh.get('line_id') != line['line_id'] or fresh.get('recipient_id') != line['recipient_id']):
            return _result('conflict', 'assisted_cart_line_coverage_changed')
        identities.add(line['line_id'])
        selection = line['source_selection']
        selected_scope = selection.get('scope') if isinstance(selection, dict) else None
        expected_scope = {'client_id': scope['client_id'], 'episode_id': scope['episode_id'],
            'session_id': original.get('session_id'), 'generation': original.get('generation'),
            'revision': original.get('selection_revision'), 'reset_floor': scope['reset_floor'],
            'line_id': line['line_id'], 'recipient_id': line['recipient_id']}
        if (not isinstance(selection, dict) or selection.get('schema') != 'source-selection.v1'
            or not isinstance(selected_scope, dict) or any(
                selected_scope.get(key) != value or type(selected_scope.get(key)) is not type(value)
                for key, value in expected_scope.items())):
            return _result('conflict', 'assisted_cart_source_scope_changed')
        custom = quote['configuration'].get('assisted_line')
        if custom and (custom['line_id'] != line['line_id'] or custom['recipient_id'] != line['recipient_id']
            or custom['original_index'] != index or custom['decision_id'] != evidence.get('decision_id')
            or custom['decision_digest'] != evidence.get('decision_digest')
            or custom['line_digest'] != evidence['line_digests'][index]):
            return _result('conflict', 'assisted_cart_approval_changed')
    return _result('ready', payload=payload)


def build_assisted_cart_binding(*, verified_decision, quoted_items, bound_episode_id=None):
    from management.services.ig_assisted_commerce import VerifiedAssistedDecision
    if type(verified_decision) is not VerifiedAssistedDecision:
        raise ValueError('assisted_decision_requires_native_producer')
    cap = verified_decision
    if (len(cap.ready_lines) != len(cap.lines) or not isinstance(quoted_items, (list, tuple))
        or len(quoted_items) != len(cap.lines)):
        raise ValueError('assisted_full_ready_required')
    original, current = cap.full_capture, cap.current_capture
    if bound_episode_id is None:
        bound_episode_id = original['scope']['episode_id']
    lines, maps = [], []
    for index, (approved, source, item) in enumerate(zip(cap.lines, original['lines'], quoted_items, strict=True)):
        config = union_configuration(item, schema=ASSISTED_SCHEMA)
        if config is None or (approved.line_id, approved.recipient_id, approved.original_index) != (
                source['line_id'], source['recipient_id'], index):
            raise ValueError('assisted_quote_configuration_invalid')
        lines.append({'line_id': approved.line_id, 'recipient_id': approved.recipient_id,
            'index': index, 'source_selection': _copy(source['source_selection'])})
        maps.append({'line_id': approved.line_id, 'recipient_id': approved.recipient_id,
            'quote_position': index, 'configuration': config, 'configuration_digest': _digest(config),
            'quantity_source_confirmed': True})
    evidence = assisted_quote_evidence(cap)
    payload = {'schema': ASSISTED_SCHEMA, 'scope': _copy(original['scope']),
        'bound_episode_id': bound_episode_id, 'original_full_capture': original,
        'current_capture': current, 'assisted_commerce': evidence,
        'source_capture_digest': original['capture_digest'],
        'checkout_item_limit': CATALOG_CHECKOUT_ITEM_LIMIT, 'lines': lines}
    payload['source_binding_digest'] = _digest(payload)
    payload['quote_line_map'] = maps
    payload['quote_binding_digest'] = _digest({'source_binding_digest': payload['source_binding_digest'],
        'quote_line_map': maps})
    checked = validate_assisted_cart_binding(payload)
    if not checked.ok:
        raise ValueError(checked.reason)
    return checked.binding


def assisted_quote_evidence(cap):
    return {'schema': 'ig-assisted-quote.v1', 'request_id': str(cap.request_id),
        'decision_id': str(cap.decision_id), 'client_id': cap.client_id, 'intention_id': str(cap.intention_id),
        'request_version': cap.request_version, 'decision_version': cap.decision_version,
        'request_digest': cap.request_digest, 'decision_digest': cap.decision_digest,
        'full_capture_digest': cap.full_capture_digest, 'snapshot_digest': cap.snapshot_digest,
        'expires_at': cap.expires_at.isoformat(), 'currency': 'UAH',
        'line_digests': [line.line_digest for line in cap.lines]}


def rebind_assisted_cart_episode(binding, episode_id):
    checked = validate_assisted_cart_binding(binding)
    if not checked.ok or not _positive(episode_id):
        raise ValueError('assisted_cart_owner_changed')
    payload = checked.binding
    if payload['scope']['episode_id'] not in {None, episode_id} or payload['bound_episode_id'] not in {None, episode_id}:
        raise ValueError('assisted_cart_owner_changed')
    payload['bound_episode_id'] = episode_id
    source = {key: value for key, value in payload.items()
        if key not in {'source_binding_digest', 'quote_line_map', 'quote_binding_digest'}}
    payload['source_binding_digest'] = _digest(source)
    payload['quote_binding_digest'] = _digest({'source_binding_digest': payload['source_binding_digest'],
        'quote_line_map': payload['quote_line_map']})
    return payload


@_finite_input
def bind_quote_lines(*, binding, quoted_items, readiness_by_line):
    """Bind ordered, authoritative quote configurations to every source line.

    readiness_by_line envelopes are owned captures: {scope, capture_digest,
    readiness: selection_readiness(..., strict=True)}. Price validation remains
    validate_checkout_items' job. No aggregation or separate shipping is implied.
    """
    payload = _binding_payload(binding)
    if not _source_digest_matches(payload):
        return _result("conflict", "cart_binding_digest_invalid")
    lines = payload["lines"]
    if not isinstance(quoted_items, (list, tuple)) or len(quoted_items) != len(lines):
        return _result("conflict", "cart_quote_line_count_changed")
    if not isinstance(readiness_by_line, dict) or set(readiness_by_line) != {line["line_id"] for line in lines}:
        return _result("partial", "cart_readiness_missing")
    maps, missing = [], []
    for position, (line, quoted) in enumerate(zip(lines, quoted_items)):
        envelope = readiness_by_line[line["line_id"]]
        if not isinstance(envelope, dict) or envelope.get("capture_digest") != payload["source_capture_digest"]:
            return _result("conflict", "cart_readiness_capture_changed")
        scope = envelope.get("scope")
        reason = _scope_reason(scope, payload["scope"])
        if reason or scope.get("line_id") != line["line_id"] or scope.get("recipient_id") != line["recipient_id"]:
            return _result("conflict", "cart_readiness_scope_changed")
        state = envelope.get("readiness")
        if not isinstance(state, dict) or state.get("applicability_known") is not True:
            missing.append(line["line_id"] + ":applicability")
            continue
        if state.get("can_issue_link") is not True or not isinstance(state.get("missing"), list) or state["missing"]:
            missing.append(line["line_id"] + ":readiness")
            continue
        try:
            configured = _configuration({"product_id": state["product"]["id"], "qty": state["quantity"],
                "size": state["size"]["selected"], "fit_option_code": state["fit"]["selected"],
                "color_variant_id": state["color"]["selected_variant_id"], "option_values": state["options"]["selected"]})
        except (KeyError, TypeError):
            configured = None
        quote_config = _configuration(quoted)
        if configured is None or configured != quote_config:
            return _result("conflict", "cart_quote_configuration_changed")
        choices = line["choices"]
        quantity = choices.get("quantity", (line["quantity_default"] or {}).get("value"))
        if choices.get("product_id") != configured["product_id"] or quantity != configured["qty"]:
            return _result("conflict", "cart_quote_choice_changed")
        for key in ("size", "fit_option_code", "color_variant_id", "option_values"):
            if key in choices:
                candidate = _configuration({**configured, key: choices[key]})
                if candidate != configured:
                    return _result("conflict", "cart_quote_choice_changed")
        maps.append({"line_id": line["line_id"], "recipient_id": line["recipient_id"],
                     "quote_position": position, "configuration": configured,
                     "configuration_digest": _digest(configured),
                     "quantity_source_confirmed": "quantity" in choices})
    if missing:
        return _result("partial", "cart_readiness_incomplete", payload=payload, missing=missing)
    payload["quote_line_map"] = maps
    payload["quote_binding_digest"] = _digest({"source_binding_digest": payload["source_binding_digest"], "quote_line_map": maps})
    return _result("ready", payload=payload)


def frozen_cart_binding_payload(binding):
    """Only a complete quote map can enter an immutable checkout revision."""
    if not isinstance(binding, CartBindingResult) or binding.status != "ready":
        raise ValueError("cart_quote_binding_not_ready")
    payload = binding.binding
    if not _source_digest_matches(payload) or not _quote_digest_matches(payload):
        raise ValueError("cart_quote_binding_digest_invalid")
    return payload


def stable_checkout_source_capture(capture):
    """Detached full DTO comparison, excluding only its owner-dependent hashes.

    This authenticates the DTO's own digest, not its current database ownership.
    The checkout owner must independently fence permission/episode/deal changes.
    """
    try:
        if not isinstance(capture, dict) or capture.get("schema") != SOURCE_SCHEMA or capture.get("status") != "captured":
            return None
        value = _copy(capture)
        supplied = value.pop("capture_digest", None)
        if not _hash(supplied) or _digest(value) != supplied:
            return None
        fence = value.get("fence")
        if not isinstance(fence, dict) or not _hash(fence.get("owner_digest")):
            return None
        fence.pop("owner_digest")
        return value
    except (TypeError, ValueError, OverflowError, RecursionError):
        return None


def same_checkout_source_capture(original, current):
    """Compare every source/cart/head field; malformed or missing DTOs abstain."""
    before = stable_checkout_source_capture(original)
    after = stable_checkout_source_capture(current)
    return before is not None and after is not None and before == after


@_finite_input
def validate_quote_source_positions(*, binding, client_id, item_specs):
    """Structural positional proof for separately priced source cart lines.

    Initial effect owners additionally validate fresh canonical capture. Frozen
    payment owners validate the exact immutable revision graph before calling.
    This neither authenticates model JSON nor grants money/send authority.
    """
    payload = _binding_payload(binding)
    if not _source_digest_matches(payload):
        return _result("conflict", "cart_binding_digest_invalid")
    if "quote_line_map" in payload and not _quote_digest_matches(payload):
        return _result("conflict", "cart_quote_binding_digest_invalid")
    scope = payload.get("scope")
    if not isinstance(scope, dict) or not _positive(client_id) or scope.get("client_id") != client_id:
        return _result("conflict", "cart_scope_changed")
    # Rebuild the existing source binding validator from its original captured
    # fields. No extra proof format or mutable duplicate permission is created.
    capture = {"schema": SOURCE_SCHEMA, "status": "captured", "scope": scope,
        "capture_digest": payload.get("source_capture_digest"),
        **{key: payload.get(key) for key in ("session_id", "generation", "selection_revision", "active_index", "active_line_id")},
        "lines": [{"line_id": line.get("line_id"), "recipient_id": line.get("recipient_id"),
            "index": line.get("index"), "source_selection": line.get("source_selection"),
            "defaults": {"quantity": line.get("quantity_default")}} for line in payload["lines"]]}
    validated = validate_cart_binding(binding=payload, current_capture=capture, expected_scope=scope)
    if not validated.ok:
        return validated
    lines = payload["lines"]
    if not isinstance(item_specs, (list, tuple)) or len(item_specs) != len(lines):
        return _result("conflict", "cart_quote_line_count_changed")
    for index, (line, item) in enumerate(zip(lines, item_specs)):
        config = _configuration(item)
        choices = line["choices"]
        quantity = choices.get("quantity", (line["quantity_default"] or {}).get("value"))
        if config is None or config["product_id"] != choices.get("product_id") or config["qty"] != quantity:
            return _result("conflict", "cart_quote_choice_changed")
        for key in ("size", "fit_option_code", "color_variant_id", "option_values"):
            if key in choices and _configuration({**config, key: choices[key]}) != config:
                return _result("conflict", "cart_quote_choice_changed")
        if "quote_line_map" in payload and payload["quote_line_map"][index]["configuration"] != config:
            return _result("conflict", "cart_quote_configuration_changed")
    return _result("captured", payload=payload)
