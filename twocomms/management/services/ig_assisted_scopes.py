"""Native whole-line scopes and conserved historical assisted intentions.

An observation dictionary, manager-selected IDs, age, or generic paid status
cannot allocate capacity. Selection requires a real delivered native choice;
coverage requires the coordinator's authenticated historical execution leaf.
"""
from copy import deepcopy
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from decimal import Decimal
import json
import re

from django.db import transaction

from management.services import ig_assisted_commerce as commerce

_ISSUER = object()
MAX_HISTORY = 32
COVERAGE_SCHEMA = "assisted-intention-coverage.v1"
SELECTION_SCHEMA = "ig-assisted-selection.v1"
SELECTION_EVIDENCE_KEYS = (
    "schema", "client_id", "intention_id", "root_request_id", "root_request_digest", "origin_full_capture_digest",
    "request_id", "decision_id", "request_digest", "decision_digest", "full_capture_digest", "decision_snapshot_digest",
    "scope", "scope_digest", "selected_line_ids", "deferred_line_ids", "settled_line_ids", "cancelled_line_ids",
    "quoted_total", "custom_quantity", "currency", "terms_digest", "shipping_terms", "delivery_scope",
    "coverage_digest", "ledger_coverage_digest", "choice_source_id", "purchase_source_id", "choice_digest",
    "purchase_digest", "line_map", "expires_at",
)


def normalize_shipping_terms(value):
    if not isinstance(value, dict) or set(value) != {"now", "later", "together"}:
        commerce._fail("shipping_terms_required")
    result = {}
    for scope in ("now", "later", "together"):
        row = value[scope]
        if (not isinstance(row, dict) or set(row) != {"payer", "customer_maximum", "currency"}
            or row.get("payer") not in {"customer", "merchant"} or row.get("currency") != "UAH"):
            commerce._fail("shipping_terms_invalid")
        text = row.get("customer_maximum")
        if isinstance(text, str) and re.fullmatch(r"0(?:\.0{1,2})?", text):
            amount = Decimal("0.00")
        else:
            amount = commerce._price(text, required=True)
        if row["payer"] == "merchant" and amount != 0:
            commerce._fail("merchant_shipping_cap_must_be_zero")
        result[scope] = {"payer": row["payer"], "customer_maximum": format(amount, ".2f"), "currency": "UAH"}
    return result


parse_shipping_terms = normalize_shipping_terms


def _source_history(request):
    """Historical ownership/text parity, without today's effect TTL/actor/head."""
    data = request.envelope["snapshot"]
    # One joined native membership/source read per ancestor. This snapshot is
    # local to this verification pass; the caller's second capture reloads it.
    members = {member.message_id: member for member in request.revision.sources
        .filter(message_id__in=[proof["message_id"] for proof in data["sources"]]).select_related("message")}
    _verify_source_history(request, members)


def _verify_source_history(request, members):
    """Pure parity check of this pass's joined native memberships and messages."""
    from management.services.ig_turn_revisions import _source_payload, _digest
    data = request.envelope["snapshot"]
    if _digest(request.revision.bundle_snapshot) != request.revision.snapshot_digest:
        commerce._fail("original_revision_changed")
    for proof in data["sources"]:
        member = members.get(proof["message_id"])
        source = member.message if member is not None else None
        if (not source or source.client_id != request.client_id
            or source.role != "user" or source.source != "webhook" or source.status == "failed"
            or source.provider_namespace != request.source_namespace
            or source.sender_id != request.client.igsid or source.pk < request.reset_floor
            or not member or member.revision_id != request.revision_id or member.source_digest != proof["digest"]
            or _source_payload(source, previous=member, ordinal=member.ordinal)["source_digest"] != proof["digest"]):
            commerce._fail("original_source_changed")


def _ancestry_rows(request):
    from django.db.models import Q, Subquery
    from management.models import IgTurnRevisionSource
    Request = commerce._models()[0]
    rows, seen = [], set()
    # Derive the family from the actual target row inside this one SQL read;
    # caller-provided client/intention/version values are never a read fence.
    identity = Request.objects.filter(pk=getattr(request, "pk", None))
    candidates = list(Request.objects.filter(
        client_id=Subquery(identity.values("client_id")[:1]),
        intention_id=Subquery(identity.values("intention_id")[:1]),
        version__lte=Subquery(identity.values("version")[:1]))
        .select_related("client", "revision").order_by("-version", "pk")[:MAX_HISTORY])
    by_id = {str(row.pk): row for row in candidates}
    actual = by_id.get(str(commerce._uuid(request.pk))) if candidates else None
    if not actual:
        commerce._fail("request_missing")
    client_id, intention = actual.client_id, actual.intention_id
    while actual:
        if actual.pk in seen or len(rows) >= MAX_HISTORY or not commerce._verified(actual, "request"):
            commerce._fail("request_ancestry_unverified")
        if actual.client_id != client_id or actual.intention_id != intention:
            commerce._fail("intention_owner_changed")
        rows.append(actual)
        seen.add(actual.pk)
        ancestry = actual.envelope["snapshot"].get("ancestry")
        if not ancestry:
            break
        parent = by_id.get(ancestry.get("parent_request_id"))
        if parent is None:
            # Superseded root versions are not ancestors. A sparse chain may
            # reference a parent outside the initial bounded version window;
            # reload that exact native row under the same chain-only limit.
            parent = Request.objects.select_related("client", "revision").filter(
                pk=ancestry.get("parent_request_id")).first()
        if (not parent or not commerce._verified(parent, "request")
            or parent.client_id != client_id or parent.intention_id != intention
            or parent.envelope_hmac != ancestry.get("parent_request_digest")
            or parent.version >= actual.version or ancestry.get("parent_version") != parent.version):
            commerce._fail("request_ancestry_unverified")
        # Direct immutable transition authentication avoids history -> adoption
        # verifier -> history recursion. Fresh adoption also runs its native leaf.
        from management.models import IgCommerceSelectionTransition
        from management.services.ig_assisted_continuation_sources import _signed_manifest
        transition = IgCommerceSelectionTransition.objects.select_related("session").filter(
            pk=(ancestry.get("adoption_proof") or {}).get("transition_id"), session__client_id=client_id).first()
        if not transition:
            commerce._fail("adoption_transition_missing")
        manifest = _signed_manifest(transition)
        if (manifest.get("parent_request_id") != str(parent.pk) or manifest.get("intention_id") != str(intention)
            or manifest.get("line_map") != ancestry.get("line_map")
            or manifest.get("coverage_digest") != ancestry.get("ledger_coverage_digest")):
            commerce._fail("adoption_ancestry_changed")
        actual = parent
    root = rows[-1]
    for row in rows[:-1]:
        ancestry = row.envelope["snapshot"]["ancestry"]
        if ancestry.get("root_request_id") != str(root.pk) or ancestry.get("root_request_digest") != root.envelope_hmac:
            commerce._fail("request_root_changed")
    # Every ancestor above came from fresh native rows and passed its HMAC,
    # owner, parent/version and transition checks. Fetch only its sealed source
    # memberships, in one bounded batch local to this single ancestry pass.
    requested = Q(pk__in=[])
    for row in rows:
        proofs = row.envelope["snapshot"]["sources"]
        if not 1 <= len(proofs) <= commerce.MAX_SOURCES:
            commerce._fail("source_coverage_bound")
        requested |= Q(revision_id=row.revision_id, message_id__in=[proof["message_id"] for proof in proofs])
    joined = list(IgTurnRevisionSource.objects.filter(requested).select_related("message")
        [:MAX_HISTORY * commerce.MAX_SOURCES + 1])
    if len(joined) > MAX_HISTORY * commerce.MAX_SOURCES:
        commerce._fail("source_coverage_bound")
    members_by_revision = {}
    for member in joined:
        members_by_revision.setdefault(member.revision_id, {})[member.message_id] = member
    for row in rows:
        _verify_source_history(row, members_by_revision.get(row.revision_id, {}))
    return root, rows


def _history(request):
    Request, Decision, Execution, _Image = commerce._models()
    root, ancestry = _ancestry_rows(request)
    earliest = Execution.objects.filter(client_id=root.client_id, intention_id=root.intention_id).select_related(
        "request__client", "request__revision", "decision").order_by("created_at", "pk").first()
    if earliest:
        if not commerce._verified(earliest, "execution"):
            commerce._fail("original_execution_unverified")
        first_request = earliest.request
        verified_ancestor = next((row for row in ancestry if row.pk == first_request.pk), None)
        if verified_ancestor is not None:
            # The joined earliest row must match this call's already verified
            # ancestry. Reuse no proof from another capture or effect boundary.
            if (not commerce._verified(first_request, "request")
                or first_request.envelope_hmac != verified_ancestor.envelope_hmac
                or first_request.envelope != verified_ancestor.envelope
                or first_request.client_id != root.client_id or first_request.intention_id != root.intention_id):
                commerce._fail("intention_root_conflict")
            first_root = root
        else:
            first_root, _rows = _ancestry_rows(first_request)
        if first_root.pk != root.pk:
            commerce._fail("intention_root_conflict")
        original_decision = earliest.decision
    else:
        original_decision = Decision.objects.filter(request=root, status="approved").order_by("-version").first()
    approved = {}
    if original_decision:
        if not commerce._verified(original_decision, "decision") or original_decision.request_id != root.pk:
            commerce._fail("original_decision_unverified")
        approved = {row["line_id"]: row["quantity"] for row in original_decision.envelope["snapshot"]["items"]}
    originals = []
    for row in root.envelope["snapshot"]["lines"]:
        quantity = approved.get(row["line_id"], row["source_choices"].get("quantity"))
        if quantity is None and row.get("quantity_default") == {
                "value": 1, "authority": "existing_cart_default", "source_confirmed": False}:
            quantity = 1
        if type(quantity) is not int or not 1 <= quantity <= commerce.MAX_QUANTITY:
            commerce._fail("original_quantity_unverified")
        originals.append({"line_id": row["line_id"], "recipient_id": row["recipient_id"],
            "original_index": row["original_index"], "quantity": quantity,
            "source_choices": deepcopy(row["source_choices"]), "source_evidence": deepcopy(row["source_evidence"])})
    return root, originals, ancestry


def original_intention_source_history(request, *, now=None):
    """Bounded read-only ancestry; grants no settlement or residual capacity."""
    try:
        root, originals, rows = _history(request)
        return {"ready": True, "reason": "", "schema": "assisted-intention-history.v1",
            "client_id": root.client_id, "intention_id": str(root.intention_id), "root_request_id": str(root.pk),
            "root_request_digest": root.envelope_hmac, "root_full_capture_digest": root.full_capture_digest,
            "root_created_at": root.created_at.isoformat(), "origin_full_capture": deepcopy(root.envelope["snapshot"]["full_capture"]),
            "original_lines": originals, "request_history": [{"request_id": str(row.pk), "request_digest": row.envelope_hmac,
                "version": row.version, "parent_request_id": (row.envelope["snapshot"].get("ancestry") or {}).get("parent_request_id")}
                for row in reversed(rows)]}
    except (commerce.AssistedDecisionError, ValueError, KeyError, TypeError) as exc:
        return {"ready": False, "reason": getattr(exc, "reason", "intention_history_unverified")}


def original_intention_history(request, *, now=None):
    history = original_intention_source_history(request, now=now)
    if not history.get("ready"):
        return history
    coverage = intention_coverage(history["client_id"], history["intention_id"], now=now)
    if not coverage.get("ready"):
        return {"ready": False, "reason": coverage.get("reason") or "intention_history_unverified"}
    return {**history, "coverage": coverage}


def _base_digest(value):
    return commerce._digest({key: item for key, item in value.items() if key not in {"ledger_digest", "coverage_digest"}})


def intention_coverage(client_id, intention_id, *, now=None, exclude_execution_id=None, lock=False, read_unit=None):
    """Every actual execution stays protected until verified native settlement.

    No expiration inference releases UNKNOWN/resource-review capacity. The
    historical execution leaf never calls this function or current request.
    """
    from management.services.ig_commercial_episodes import commercial_episode_client_lock
    from management.models import IgClient
    Request, _Decision, Execution, _Image = commerce._models()
    try:
        intention_id, now = commerce._uuid(intention_id), commerce._now(now)
        with (commercial_episode_client_lock(client_id) if lock else nullcontext()), (transaction.atomic() if lock else nullcontext()):
            client = (IgClient.objects.select_for_update() if lock else IgClient.objects).filter(pk=client_id).first()
            head = Request.objects.filter(client_id=client_id, intention_id=intention_id).order_by("-version").first()
            if not client or not head or client.privacy_erasure_started_at or client.hidden_at:
                commerce._fail("intention_unavailable")
            if read_unit is not None:
                require_selection_read_unit(read_unit, head, now=now)
            root, originals, _ancestry = (read_unit._history(head, now=now) if read_unit is not None else _history(head))
            quantities = {line["line_id"]: line["quantity"] for line in originals}
            rows = Execution.objects.filter(client_id=client_id, intention_id=intention_id).select_related(
                "request", "decision", "proposal", "client", "episode", "order").order_by("created_at", "pk")
            if lock:
                rows = rows.select_for_update()
            rows = list(rows[:MAX_HISTORY + 1])
            if len(rows) > MAX_HISTORY:
                commerce._fail("execution_history_bound")
            settled, inflight, cancelled, orders, executions, consumed = [], [], [], [], [], set()
            for execution in rows:
                from management.services.ig_assisted_checkout import validate_assisted_execution_history
                checked = validate_assisted_execution_history(execution, now=now)
                if not checked.get("ready"):
                    commerce._fail(checked.get("reason") or "execution_history_unverified")
                body = execution.envelope["snapshot"]
                evidence = body.get("selection_evidence")
                ids = list(evidence["selected_line_ids"] if evidence else body["selected_line_ids"])
                if (not ids or len(set(ids)) != len(ids) or any(identity not in quantities for identity in ids)
                    or consumed.intersection(ids)):
                    commerce._fail("intention_quantity_overlap")
                approved_rows = ((execution.decision.envelope["snapshot"].get("scoped_quotes") or {}).get("selected") or {}).get("lines")
                if execution.scope == "full":
                    approved_rows = execution.decision.envelope["snapshot"]["items"]
                actual_quantities = {row["line_id"]: row["quantity"] for row in approved_rows or []}
                if any(actual_quantities.get(identity) != quantities[identity] for identity in ids):
                    commerce._fail("intention_quantity_changed")
                consumed.update(ids)
                if execution.order_id is not None:
                    if not execution.settled_at or execution.active_slot is not None or execution.released_at:
                        commerce._fail("settled_execution_projection_unverified")
                    state = "settled"
                    settled.extend(ids)
                    orders.append({"execution_id": str(execution.pk), "episode_id": execution.episode_id,
                        "order_id": execution.order_id, "line_ids": ids, "quantities": {identity: quantities[identity] for identity in ids}})
                elif execution.active_slot == 1 and not execution.released_at and not execution.settled_at:
                    state = "inflight"
                    if exclude_execution_id is None or str(execution.pk) != str(exclude_execution_id):
                        inflight.extend(ids)
                else:
                    # Safe payment release is not customer cancellation of the
                    # wishlist. No cancellation producer exists in this slice.
                    commerce._fail("released_intention_requires_review")
                executions.append({"execution_id": str(execution.pk), "execution_digest": execution.envelope_hmac,
                    "episode_id": execution.episode_id, "request_id": str(execution.request_id),
                    "decision_id": str(execution.decision_id), "scope": execution.scope, "line_ids": ids,
                    "quantities": {identity: quantities[identity] for identity in ids}, "state": state})
            # Excluding self is admission for its already reserved exact scope;
            # it never erases history or grants another proposal factory scope.
            excluded = set()
            if exclude_execution_id is not None:
                excluded = {identity for row in executions if row["execution_id"] == str(exclude_execution_id) and row["state"] == "inflight"
                    for identity in row["line_ids"]}
            unavailable = set(settled) | set(inflight) | set(cancelled)
            payload = {"schema": COVERAGE_SCHEMA, "client_id": client_id, "intention_id": str(intention_id),
                "root_request_id": str(root.pk), "root_request_digest": root.envelope_hmac, "original_quantities": quantities,
                "selected_line_ids": [], "settled_line_ids": [line["line_id"] for line in originals if line["line_id"] in settled],
                "inflight_line_ids": [line["line_id"] for line in originals if line["line_id"] in inflight],
                "cancelled_line_ids": cancelled,
                "deferred_line_ids": [line["line_id"] for line in originals if line["line_id"] not in unavailable],
                "orders": orders, "executions": executions}
            digest = _base_digest(payload)
            return {"ready": True, "reason": "", **payload, "ledger_digest": digest, "coverage_digest": digest}
    except (commerce.AssistedDecisionError, ValueError, KeyError, TypeError) as exc:
        return {"ready": False, "reason": getattr(exc, "reason", "intention_coverage_unverified")}


def request_line_map(request):
    data = request.envelope["snapshot"]
    ancestry = data.get("ancestry")
    if ancestry:
        mapping = deepcopy(ancestry["line_map"])
    else:
        mapping = [{"line_id": row["line_id"], "recipient_id": row["recipient_id"], "original_index": row["original_index"],
            "current_line_id": row["line_id"], "current_index": row["original_index"]} for row in data["lines"]]
    if (len(mapping) != len(data["lines"]) or len({row["line_id"] for row in mapping}) != len(mapping)
        or len({row["current_line_id"] for row in mapping}) != len(mapping)):
        commerce._fail("request_line_map_invalid")
    for index, (entry, current) in enumerate(zip(mapping, data["lines"], strict=True)):
        if entry["current_index"] != index or entry["current_line_id"] != current["line_id"] or entry["recipient_id"] != current["recipient_id"]:
            commerce._fail("request_line_map_changed")
    return mapping


def build_scoped_quotes(request, lines, shipping_terms, *, read_unit=None, now=None):
    """Native selected-only price observation sealed on exact draft approval."""
    terms = normalize_shipping_terms(shipping_terms)
    mapping = request_line_map(request)
    ready = [line for line in lines if line.availability == "ready"]
    if not ready:
        commerce._fail("selected_ready_lines_required")
    if read_unit is not None:
        unit = require_selection_read_unit(read_unit, request, now=now)
        root, originals, _ancestry = unit._history(request, now=now)
    else:
        root, originals, _ancestry = _history(request)
    quantities = {line["line_id"]: line["quantity"] for line in originals}
    by_current = {entry["current_line_id"]: entry for entry in mapping}
    specs = {line.line_id: deepcopy(next(row for row in request.envelope["snapshot"]["lines"]
        if row["line_id"] == line.line_id)["catalog_item_spec"]) for line in ready if line.item_kind == "catalog_apparel"}
    priced = commerce._canonical_catalog_observations(specs)
    selected, line_map = [], []
    for line in ready:
        entry = by_current[line.line_id]
        if request.envelope["snapshot"].get("ancestry") and line.quantity != quantities.get(entry["line_id"]):
            commerce._fail("residual_quantity_changed")
        selected.append(replace(line, line_id=entry["line_id"], original_index=entry["original_index"],
            unit_price=commerce._price(priced[line.line_id]["unit_price"], required=True)
                if line.item_kind == "catalog_apparel" else line.unit_price))
        line_map.append({**entry, "quote_position": len(line_map)})
    selected_ids = [line.line_id for line in selected]
    deferred_ids = [entry["line_id"] for entry in mapping if entry["line_id"] not in selected_ids]
    scope_digest = commerce._digest({"scope": "partial", "selected_line_ids": selected_ids, "deferred_line_ids": deferred_ids})
    total = sum((line.ready_total for line in selected), Decimal("0.00"))
    custom = sum(line.quantity for line in selected if line.item_kind == "custom_apparel")
    if not 0 < total <= commerce.MAX_VALUE or sum(line.quantity for line in selected) > commerce.MAX_QUANTITY:
        commerce._fail("selected_scope_checkout_bounds")
    delivery = "later" if request.envelope["snapshot"].get("ancestry") else "now"
    proof = {"scope_digest": scope_digest, "total": format(total, ".2f"), "custom_quantity": custom,
        "lines": [line.as_dict() for line in selected], "shipping_terms": terms, "delivery_scope": delivery}
    return {"selected": {"scope": "partial", "selected_line_ids": selected_ids, "deferred_line_ids": deferred_ids,
        "scope_digest": scope_digest, "total": proof["total"], "custom_quantity": custom, "currency": "UAH",
        "lines": proof["lines"], "line_map": line_map, "shipping_terms": terms, "delivery_scope": delivery,
        "terms_digest": commerce._digest(proof)}}


def _quote_lines(request, decision, *, parent_lines=None):
    quote = (decision.envelope["snapshot"].get("scoped_quotes") or {}).get("selected")
    if not isinstance(quote, dict) or quote.get("scope") != "partial":
        commerce._fail("partial_terms_required")
    parent = parent_lines if parent_lines is not None else commerce.normalize_assisted_lines(decision.envelope["snapshot"]["items"],
        source_lines=request.envelope["snapshot"]["lines"], questions=request.envelope["snapshot"]["questions"], require_ready_answers=True)
    mapping = request_line_map(request)
    expected = []
    for line in parent:
        if line.availability != "ready":
            continue
        entry = next(row for row in mapping if row["current_line_id"] == line.line_id)
        saved = next((row for row in quote.get("lines", []) if row.get("line_id") == entry["line_id"]), None)
        if not saved:
            commerce._fail("selected_quote_line_missing")
        amount = commerce._price(saved.get("unit_price"), required=True)
        scoped = replace(line, line_id=entry["line_id"], original_index=entry["original_index"], unit_price=amount)
        if saved != scoped.as_dict() or line.item_kind == "custom_apparel" and amount != line.unit_price:
            commerce._fail("selected_quote_line_changed")
        expected.append(scoped)
    if (not expected or len(expected) != len(quote.get("lines", []))
        or quote.get("selected_line_ids") != [line.line_id for line in expected]
        or quote.get("deferred_line_ids") != [entry["line_id"] for entry in mapping if entry["line_id"] not in quote["selected_line_ids"]]
        or quote.get("line_map") != [{**row, "quote_position": position} for position, row in enumerate(
            [entry for entry in mapping if entry["line_id"] in quote["selected_line_ids"]])]):
        commerce._fail("selected_quote_scope_changed")
    scope = {"scope": "partial", "selected_line_ids": quote["selected_line_ids"], "deferred_line_ids": quote["deferred_line_ids"]}
    terms = normalize_shipping_terms(quote["shipping_terms"])
    total = sum((line.ready_total for line in expected), Decimal("0.00"))
    custom = sum(line.quantity for line in expected if line.item_kind == "custom_apparel")
    preimage = {"scope_digest": quote["scope_digest"], "total": quote["total"], "custom_quantity": quote["custom_quantity"],
        "lines": quote["lines"], "shipping_terms": terms, "delivery_scope": quote["delivery_scope"]}
    if (quote["scope_digest"] != commerce._digest(scope) or quote["terms_digest"] != commerce._digest(preimage)
        or quote["total"] != format(total, ".2f") or quote["custom_quantity"] != custom or quote.get("currency") != "UAH"
        or quote["delivery_scope"] not in {"now", "later", "together"} or total <= 0 or total > commerce.MAX_VALUE):
        commerce._fail("selected_quote_terms_changed")
    return quote, tuple(expected)


@dataclass(frozen=True)
class VerifiedAssistedSelection:
    decision: commerce.VerifiedAssistedDecision
    lines: tuple
    _quote_json: str
    _coverage_json: str
    _choice_json: str
    _purchase_json: str
    _origin_json: str
    _current_capture_json: str
    root_request_id: object
    root_request_digest: str
    origin_full_capture_digest: str
    execution_id: object | None
    proof_scope: str
    _issuer: object = field(repr=False, compare=False)

    def __post_init__(self):
        if self._issuer is not _ISSUER or self.proof_scope not in {"unconsumed", "execution", "historical"}:
            commerce._fail("verified_selection_requires_native_producer")

    def __getattr__(self, name):
        if name in {"request_id", "decision_id", "client_id", "actor_id", "approver_id", "intention_id", "request_version",
                "decision_version", "source_revision_id", "request_digest", "decision_digest", "full_capture_digest",
                "snapshot_digest", "physical_checked_at", "expires_at", "currency", "source_proof", "full_capture"}:
            return getattr(self.decision, name)
        raise AttributeError(name)

    @property
    def quote(self): return json.loads(self._quote_json)
    @property
    def selected_lines(self): return self.lines
    @property
    def ready_lines(self): return self.lines
    @property
    def scope(self): return "partial"
    @property
    def historical(self): return self.proof_scope == "historical"
    @property
    def approved_total(self): return Decimal(self.quote["total"])
    @property
    def approved_custom_quantity(self): return self.quote["custom_quantity"]
    @property
    def scope_digest(self): return self.quote["scope_digest"]
    @property
    def terms_digest(self): return self.quote["terms_digest"]
    @property
    def selected_line_ids(self): return list(self.quote["selected_line_ids"])
    @property
    def deferred_line_ids(self): return list(self.quote["deferred_line_ids"])
    @property
    def line_map(self): return deepcopy(self.quote["line_map"])
    @property
    def current_selected_line_ids(self): return [row["current_line_id"] for row in self.line_map]
    @property
    def shipping_terms(self): return deepcopy(self.quote["shipping_terms"])
    @property
    def delivery_scope(self): return self.quote["delivery_scope"]
    @property
    def shipping_scope(self): return self.delivery_scope
    @property
    def original_request_id(self): return self.root_request_id
    @property
    def coverage(self): return json.loads(self._coverage_json)
    @property
    def coverage_digest(self): return self.coverage["coverage_digest"]
    @property
    def ledger_coverage_digest(self): return self.coverage["ledger_digest"]
    @property
    def choice_proof(self): return json.loads(self._choice_json)
    @property
    def purchase_proof(self): return json.loads(self._purchase_json)
    @property
    def choice_source_id(self): return self.choice_proof["source_id"]
    @property
    def purchase_source_id(self): return self.purchase_proof["message_id"]
    @property
    def revision_id(self): return self.purchase_proof["revision_id"]
    @property
    def origin_full_capture(self): return json.loads(self._origin_json)
    @property
    def current_capture(self): return json.loads(self._current_capture_json)


def selection_evidence(value):
    if not isinstance(value, VerifiedAssistedSelection) or value._issuer is not _ISSUER:
        commerce._fail("verified_selection_requires_native_producer")
    payload = {"schema": SELECTION_SCHEMA, "client_id": value.client_id, "intention_id": str(value.intention_id),
        "root_request_id": str(value.root_request_id), "root_request_digest": value.root_request_digest,
        "origin_full_capture_digest": value.origin_full_capture_digest, "request_id": str(value.request_id),
        "decision_id": str(value.decision_id), "request_digest": value.request_digest, "decision_digest": value.decision_digest,
        "full_capture_digest": value.full_capture_digest, "decision_snapshot_digest": value.snapshot_digest,
        "scope": value.scope, "scope_digest": value.scope_digest, "selected_line_ids": value.selected_line_ids,
        "deferred_line_ids": value.deferred_line_ids, "settled_line_ids": value.coverage["settled_line_ids"],
        "cancelled_line_ids": value.coverage["cancelled_line_ids"], "quoted_total": format(value.approved_total, ".2f"),
        "custom_quantity": value.approved_custom_quantity, "currency": "UAH", "terms_digest": value.terms_digest,
        "shipping_terms": value.shipping_terms, "delivery_scope": value.delivery_scope, "coverage_digest": value.coverage_digest,
        "ledger_coverage_digest": value.ledger_coverage_digest, "choice_source_id": value.choice_source_id,
        "purchase_source_id": value.purchase_source_id, "choice_digest": commerce._digest(value.choice_proof),
        "purchase_digest": commerce._digest(value.purchase_proof), "line_map": value.line_map, "expires_at": value.expires_at.isoformat()}
    return {key: payload[key] for key in SELECTION_EVIDENCE_KEYS}


def _selected_coverage(base, quote):
    payload = {key: deepcopy(value) for key, value in base.items() if key not in {"ready", "reason"}}
    selected = quote["selected_line_ids"]
    if (payload["inflight_line_ids"] or set(selected) - set(payload["deferred_line_ids"])
        or set(selected) & (set(payload["settled_line_ids"]) | set(payload["cancelled_line_ids"]))):
        commerce._fail("selected_capacity_already_consumed")
    payload["selected_line_ids"] = list(selected)
    payload["deferred_line_ids"] = [identity for identity in payload["deferred_line_ids"] if identity not in selected]
    payload["coverage_digest"] = commerce._digest({key: value for key, value in payload.items() if key != "coverage_digest"})
    return payload


class AssistedSelectionReadUnit:
    """One active locked read DAG, never a persisted or cross-call authority.

    Each memo entry is produced by the ordinary native verifier. Source/card
    authentication remains mandatory; this object only shares that call's
    already verified parent and coverage between its readers.
    """
    def __init__(self, client_id, now, issuer):
        if issuer is not _ISSUER:
            commerce._fail("native_selection_read_unit_required")
        self.client_id, self.now, self._issuer = client_id, now, issuer
        self._active = True
        self._decisions, self._coverages, self._histories = {}, {}, {}

    def _require(self, request, now):
        if (self._issuer is not _ISSUER or not self._active or request.client_id != self.client_id
            or commerce._now(now) != self.now):
            commerce._fail("selection_read_unit_inactive")

    def close(self):
        self._active = False
        self._decisions.clear()
        self._coverages.clear()
        self._histories.clear()

    def decision(self, request, decision, *, actor, now):
        self._require(request, now)
        if decision.request_id != request.pk or decision.client_id != self.client_id:
            commerce._fail("selection_read_unit_owner_changed")
        key = (request.pk, request.envelope_hmac, decision.pk, decision.envelope_hmac)
        if key not in self._decisions:
            self._decisions[key] = commerce.load_verified_assisted_decision(request.pk, decision.pk, actor=actor,
                expected_request_digest=request.envelope_hmac, expected_decision_digest=decision.envelope_hmac,
                now=self.now, read_unit=self)
        cap = self._decisions[key]
        if getattr(actor, "pk", None) != cap.actor_id:
            executing = commerce._actor(getattr(actor, "pk", None))
            cap = replace(cap, actor_id=executing.pk)
        return cap

    def _history(self, request, *, now):
        self._require(request, now)
        key = (request.pk, request.envelope_hmac)
        if key not in self._histories:
            self._histories[key] = _history(request)
        return self._histories[key]

    def history(self, request, *, now):
        root, originals, rows = self._history(request, now=now)
        return {"ready": True, "reason": "", "root_request_id": str(root.pk),
            "root_request_digest": root.envelope_hmac, "original_lines": deepcopy(originals),
            "request_history": [{"request_id": str(row.pk), "version": row.version} for row in reversed(rows)]}

    def coverage(self, request, *, now):
        self._require(request, now)
        key = request.intention_id
        if key not in self._coverages:
            self._coverages[key] = intention_coverage(self.client_id, key, now=self.now, read_unit=self)
        return deepcopy(self._coverages[key])


def require_selection_read_unit(value, request, *, now):
    if not isinstance(value, AssistedSelectionReadUnit) or value._issuer is not _ISSUER:
        commerce._fail("native_selection_read_unit_required")
    value._require(request, now)
    return value


def verified_selection_from_choice(verified_choice, *, actor=None, now=None):
    from management.services.ig_assisted_partial import (revalidate_verified_partial_choice, VerifiedAssistedChoice,
        _ISSUER as choice_issuer)
    from management.services.ig_commercial_episodes import commercial_episode_client_lock
    from management.models import IgClient
    now = commerce._now(now)
    Request, Decision, _Execution, _Image = commerce._models()
    # The native choice validator authenticates these IDs again. A supplied
    # client hint only chooses the lock; it cannot issue a selection.
    if not isinstance(verified_choice, VerifiedAssistedChoice) or verified_choice._issuer is not choice_issuer:
        commerce._fail("native_partial_choice_required")
    client_id = verified_choice.client_id
    with commercial_episode_client_lock(client_id), transaction.atomic():
        IgClient.objects.select_for_update().get(pk=client_id)
        unit = AssistedSelectionReadUnit(client_id, now, _ISSUER)
        try:
            chosen = revalidate_verified_partial_choice(verified_choice, now=now, read_unit=unit)
            request = Request.objects.select_related("client").get(pk=chosen["request_id"], client_id=client_id)
            decision = Decision.objects.get(pk=chosen["decision_id"], request=request)
            cap = unit.decision(request, decision, actor=actor or decision.actor, now=now)
            quote, lines = _quote_lines(request, decision, parent_lines=cap.lines)
            if chosen["terms_digest"] != quote["terms_digest"] or chosen["choice_proof"]["source_id"] != chosen["choice_source_id"]:
                commerce._fail("native_choice_terms_changed")
            # The current card guard validated this exact sealed scoped price
            # using the same fully fresh parent. It is not a cached card fact.
            base = unit.coverage(request, now=now)
            if not base.get("ready"):
                commerce._fail(base.get("reason") or "coverage_unverified")
            coverage = _selected_coverage(base, quote)
            root, _originals, _ancestry = unit._history(request, now=now)
            return VerifiedAssistedSelection(cap, lines, commerce._json(quote), commerce._json(coverage),
                commerce._json(chosen["choice_proof"]), commerce._json(chosen["purchase_proof"]),
                commerce._json(root.envelope["snapshot"]["full_capture"]), commerce._json(cap.current_capture),
                root.pk, root.envelope_hmac, root.full_capture_digest, None, "unconsumed", _ISSUER)
        finally:
            unit.close()


def verified_selection_from_evidence(request, decision, execution, *, historical=False):
    """Actual signed historical leaf; never coverage/current/history recursion."""
    Request, Decision, Execution, _Image = commerce._models()
    if not isinstance(execution, Execution) or not commerce._verified(execution, "execution"):
        commerce._fail("selection_execution_unverified")
    actual = Execution.objects.select_related("request", "decision").get(pk=execution.pk)
    if actual.request_id != getattr(request, "pk", None) or actual.decision_id != getattr(decision, "pk", None):
        commerce._fail("selection_execution_owner_changed")
    cap = commerce.verified_assisted_decision_from_evidence(actual.request, actual.decision, historical=historical)
    quote, lines = _quote_lines(actual.request, actual.decision, parent_lines=cap.lines)
    body = actual.envelope["snapshot"]
    frozen = body.get("selection_evidence") or {}
    root = Request.objects.filter(pk=frozen.get("root_request_id"), client_id=actual.client_id, intention_id=actual.intention_id).first()
    if (not root or not commerce._verified(root, "request") or root.envelope_hmac != frozen.get("root_request_digest")
        or actual.scope != "partial" or actual.scope_digest != quote["scope_digest"]
        or actual.quoted_total != Decimal(quote["total"]) or actual.quoted_custom_quantity != quote["custom_quantity"]
        or actual.choice_source_id != (body.get("choice_proof") or {}).get("source_id")
        or actual.purchase_source_id != (body.get("purchase_proof") or {}).get("message_id")):
        commerce._fail("selection_execution_terms_changed")
    value = VerifiedAssistedSelection(cap, lines, commerce._json(quote), commerce._json(body["coverage"]),
        commerce._json(body["choice_proof"]), commerce._json(body["purchase_proof"]),
        commerce._json(root.envelope["snapshot"]["full_capture"]), commerce._json(body["current_quote_capture"]),
        root.pk, root.envelope_hmac, root.full_capture_digest, actual.pk, "historical" if historical else "execution", _ISSUER)
    if selection_evidence(value) != frozen:
        commerce._fail("selection_execution_proof_changed")
    return value


def revalidate_verified_assisted_selection(value, *, client=None, actor=None, now=None):
    if not isinstance(value, VerifiedAssistedSelection) or value._issuer is not _ISSUER or value.historical:
        commerce._fail("verified_selection_requires_native_producer")
    if client is not None and getattr(client, "pk", None) != value.client_id:
        commerce._fail("selection_client_changed")
    if value.proof_scope == "execution":
        Request, Decision, Execution, _Image = commerce._models()
        execution = Execution.objects.select_related("request", "decision").get(pk=value.execution_id, client_id=value.client_id)
        return verified_selection_from_evidence(execution.request, execution.decision, execution)
    from management.services.ig_assisted_partial import verified_partial_choice_for_source
    choice = verified_partial_choice_for_source(value.choice_source_id, now=now)
    fresh = verified_selection_from_choice(choice, actor=actor, now=now)
    if selection_evidence(fresh) != selection_evidence(value):
        commerce._fail("selection_proof_changed")
    return fresh


def admit_selected_scope(value, *, exclude_execution_id=None, now=None, lock=False):
    if not isinstance(value, VerifiedAssistedSelection) or value._issuer is not _ISSUER:
        return {"ready": False, "reason": "verified_selection_requires_native_producer"}
    if exclude_execution_id is not None and (value.execution_id is None or str(value.execution_id) != str(exclude_execution_id)):
        return {"ready": False, "reason": "selection_execution_exclusion_changed"}
    coverage = intention_coverage(value.client_id, value.intention_id, now=now, exclude_execution_id=exclude_execution_id, lock=lock)
    if not coverage.get("ready"):
        return coverage
    if set(value.selected_line_ids) & (set(coverage["settled_line_ids"]) | set(coverage["inflight_line_ids"]) | set(coverage["cancelled_line_ids"])):
        return {"ready": False, "reason": "selected_capacity_already_consumed", "coverage": coverage}
    if any(coverage["original_quantities"].get(line.line_id) != line.quantity for line in value.lines):
        return {"ready": False, "reason": "selected_quantity_changed", "coverage": coverage}
    return {"ready": True, "reason": "", "coverage": coverage}


def validate_request_ancestry(request, *, client=None, now=None, lock=False, read_unit=None):
    """Fresh R2 authority needs actual adopted current scope and settled past."""
    from management.services.ig_assisted_continuation_sources import verify_assisted_adoption_transition
    try:
        if read_unit is not None:
            require_selection_read_unit(read_unit, request, now=now)
        ancestry = request.envelope["snapshot"].get("ancestry")
        if not ancestry or client is not None and client.pk != request.client_id:
            commerce._fail("residual_ancestry_required")
        proof = verify_assisted_adoption_transition((ancestry["adoption_proof"])["transition_id"], now=now)
        if not proof.get("ready") or proof.get("line_map") != ancestry["line_map"]:
            commerce._fail("adoption_native_proof_invalid")
        coverage = (read_unit.coverage(request, now=now) if read_unit is not None else
            intention_coverage(request.client_id, request.intention_id, now=now, lock=lock))
        if (not coverage.get("ready") or coverage["inflight_line_ids"]
            or not coverage["settled_line_ids"] or coverage["ledger_digest"] != ancestry["ledger_coverage_digest"]
            or set(row["line_id"] for row in ancestry["line_map"]) != set(coverage["deferred_line_ids"])):
            commerce._fail("residual_coverage_changed")
        if read_unit is not None:
            read_unit._history(request, now=now)
        else:
            _history(request)
        return {"ready": True, "reason": "", "adoption_proof": proof, "coverage": coverage}
    except (commerce.AssistedDecisionError, ValueError, KeyError, TypeError) as exc:
        return {"ready": False, "reason": getattr(exc, "reason", "residual_ancestry_unverified")}


def validate_continuation_capture(cart, *, client, proof, now=None):
    """Dedicated native adoption, never a widened ordinary source authority."""
    from management.services.ig_assisted_continuation_sources import verify_assisted_adoption_transition
    checked = verify_assisted_adoption_transition(proof["transition_id"], now=now)
    if (not checked.get("ready") or checked.get("line_map") != proof.get("line_map")
        or cart.get("status") != "captured" or not cart.get("coverage_complete")
        or cart.get("scope", {}).get("client_id") != client.pk
        or cart.get("session_id") != checked.get("session_id")
        or [row["line_id"] for row in cart.get("lines", [])] != [row["current_line_id"] for row in checked["line_map"]]):
        commerce._fail("continuation_capture_unverified")
    return checked
