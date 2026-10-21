"""Exact ALL-WAIT assisted facts carried by the native manager-update outbox.

The original assisted request and approval remain their own immutable types.
This adapter grants notification facts only, without a customer turn, catalog
substitution, purchase capability, or a second delivery mechanism.
"""
from copy import deepcopy
import hmac
import re
import uuid

from django.db import transaction

from management.services import ig_assisted_commerce as commerce
from management.services import ig_assisted_scopes as scopes
from management.services import ig_restock_waiting_list as waiting

SOURCE_KIND = "assisted_manager_update.v1"
POLICY_VERSION = SOURCE_KIND
CONTEXT_KEY = SOURCE_KIND
ORIGIN = "fulfillment_manager_update"
_EVENT_KEYS = {"origin", "source_kind", "request_id", "request_digest", "decision_id",
    "decision_version", "decision_digest"}
_READ_CASE_ISSUER = object()


def is_task(task):
    """Recognize only this native purpose and its explicit source type."""
    data = getattr(task, "event_payload", None)
    return bool(isinstance(data, dict) and data.get("source_kind") == SOURCE_KIND
        and task.reason == ORIGIN and task.kind == "fulfillment" and task.trigger == "event"
        and task.policy_version == POLICY_VERSION)


def _payload(request, decision):
    return {"origin": ORIGIN, "source_kind": SOURCE_KIND, "request_id": str(request.pk),
        "request_digest": request.envelope_hmac, "decision_id": str(decision.pk),
        "decision_version": decision.version, "decision_digest": decision.envelope_hmac}


def _task_binding(task):
    return {"task_id": task.pk, "client_id": task.client_id, "event_key": task.event_key,
        "event_payload": deepcopy(task.event_payload), "policy_version": task.policy_version}


def _snapshot(request):
    return request.envelope["snapshot"]


def source_snapshot(request):
    """Return the actual original source after native membership verification."""
    scopes._source_history(request)
    source = request.source
    if (not source or source.pk != _snapshot(request)["source_id"]
        or source.pk not in [proof["message_id"] for proof in _snapshot(request)["sources"]]):
        raise ValueError("assisted_update_source_binding_changed")
    return source


def locale(request):
    """Use the language frozen from the original owned request sources."""
    language = deepcopy(_snapshot(request)["reply_language"])
    value = language.get("template_family")
    if value not in {"uk", "ru", "en"}:
        value = language.get("knowledge_locale")
    if value not in {"uk", "ru", "en"}:
        raise ValueError("assisted_update_language_unverified")
    return value, language


def _eligible(request, decision):
    body = decision.envelope["snapshot"]
    rows = body["items"]
    originals = _snapshot(request)["lines"]
    if (decision.status != "approved" or not rows or len(rows) != len(originals)
        or any(row["availability"] != "wait" for row in rows)
        or not any(row["item_kind"] == "custom_apparel" for row in rows)
        or [row["line_id"] for row in rows] != [row["line_id"] for row in originals]):
        raise ValueError("assisted_update_all_wait_required")


def _original_target(request, decision):
    """Pure target parity from signed native terms; no current catalog reads."""
    _eligible(request, decision)
    items = []
    for row in decision.envelope["snapshot"]["items"]:
        catalog = None
        if row["item_kind"] == "catalog_apparel":
            catalog = {"product_id": row["product_id"], "color_variant_id": row["color_variant_id"],
                "size": row["size"], "fit_option_code": row["fit_code"],
                "option_values": deepcopy(row["option_values"]), "quantity": row["quantity"]}
        items.append({"line_id": row["line_id"], "recipient_id": row["recipient_id"],
            "item_kind": row["item_kind"], "title": row["title"], "color": row["color"],
            "size": row["size"], "fit": row["fit_code"], "options": deepcopy(row["option_values"]),
            "quantity": row["quantity"], "catalog_target": catalog,
            "source": {"kind": "assisted_intention", "request_id": str(request.pk),
                "request_digest": request.envelope_hmac, "decision_id": str(decision.pk),
                "decision_digest": decision.envelope_hmac, "intention_id": str(request.intention_id)}})
    return waiting.validate_target({"schema": waiting.SCHEMA, "completion": "all", "items": items})


def target_parity(inv, request, decision):
    try:
        return inv.target == _original_target(request, decision)
    except (ValueError, KeyError, TypeError, AttributeError):
        return False


def _historical_rows(task):
    """Authenticate real rows and original source ancestry, without fresh TTL."""
    data = task.event_payload or {}
    if not is_task(task) or set(data) != _EVENT_KEYS:
        raise ValueError("assisted_update_task_binding_changed")
    request_id, decision_id = uuid.UUID(str(data.get("request_id"))), uuid.UUID(str(data.get("decision_id")))
    _Request, Decision, _Execution, _Image = commerce._models()
    decision = Decision.objects.select_related("request__client", "request__source", "request__revision", "request__task").filter(
        pk=decision_id, request_id=request_id, client_id=task.client_id).first()
    request = decision.request if decision else None
    if (not request or not decision or request.client_id != task.client_id or not request.source_id or not request.revision_id
        or request.client.privacy_erasure_started_at or data != _payload(request, decision)
        or type(data["decision_version"]) is not int
        or task.event_key != f"ig-assisted-manager-update:{request.pk}:{decision.version}"):
        raise ValueError("assisted_update_task_binding_changed")
    # This native reader reloads the exact signed approved terms. Historical
    # proof grants no fresh capacity; mutable admission is checked separately.
    commerce.verified_assisted_decision_from_evidence(request, decision, historical=True)
    source = _snapshot(request)
    if (request.source_id != source["source_id"] or request.revision_id != source["revision_id"]
        or request.full_capture_digest != source["full_capture"]["capture_digest"]
        or request.source_namespace != source["full_capture"]["scope"]["source_namespace"]
        or request.reset_floor != source["full_capture"]["scope"]["reset_floor"]
        or request.revision.snapshot_digest != source["revision_digest"]
        or request.client_permission_epoch != source["client_permission_epoch"]
        or request.source_id not in [proof["message_id"] for proof in source["sources"]]):
        raise ValueError("assisted_update_source_binding_changed")
    scopes._source_history(request)
    _eligible(request, decision)
    return request, decision


def _capture(task, request, decision):
    frozen = (task.manager_context or {}).get(CONTEXT_KEY)
    if (not isinstance(frozen, dict) or set(frozen) != {"body", "key_id", "hmac"}
        or not isinstance(frozen["key_id"], str) or not isinstance(frozen["hmac"], str)):
        raise ValueError("assisted_update_capture_unverified")
    body = frozen["body"]
    expected = commerce._sign("manager_update", body, frozen["key_id"])
    if (not expected or not hmac.compare_digest(expected, frozen["hmac"])
        or not isinstance(body, dict) or set(body) != {"schema", "binding", "target", "baseline", "configuration", "reply_language", "locale"}
        or body["schema"] != SOURCE_KIND or body["binding"] != _task_binding(task)
        or body["target"] != _original_target(request, decision)
        or body["configuration"] != commerce._configuration_capture(_snapshot(request)["full_capture"])
        or (body["locale"], body["reply_language"]) != locale(request)):
        raise ValueError("assisted_update_capture_unverified")
    ids = {item["line_id"] for item in body["target"]["items"]}
    if (not isinstance(body["baseline"], dict) or set(body["baseline"]) != ids
        or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
            for value in body["baseline"].values())):
        raise ValueError("assisted_update_configuration_unverified")
    for item in body["target"]["items"]:
        if item["item_kind"] == "custom_apparel" and body["baseline"][item["line_id"]] != waiting._digest(
                {key: value for key, value in item.items() if key != "source"}):
            raise ValueError("assisted_update_configuration_unverified")
    return body


def _projection(task, request, decision):
    frozen = _capture(task, request, decision)
    source = _snapshot(request)
    rows = decision.envelope["snapshot"]["items"]
    def timing(value):
        return {**deepcopy(value), "anchor_at": decision.created_at.isoformat()} if value is not None else None
    return {"ready": True, "reason": "", "status": "reviewed", "waiting_list": deepcopy(frozen["target"]),
        "current_request": {"id": str(request.pk), "scope": "assisted_cart", "client_id": request.client_id,
            "source_id": request.source_id, "source_namespace": request.source_namespace, "locale": frozen["locale"],
            "reply_language": deepcopy(frozen["reply_language"]), "sources": deepcopy(source["sources"]),
            "settings_id": source["settings_id"], "settings_epoch": source["settings_permission_epoch"],
            "client_epoch": source["client_permission_epoch"], "customer_requirements": [],
            "lines": [{"line_id": item["line_id"], "recipient_id": item["recipient_id"],
                "label": waiting.item_label(item)} for item in frozen["target"]["items"]]},
        "decision": {"id": str(decision.pk), "version": decision.version,
            "lines": [{"line_id": row["line_id"], "availability": "wait",
                "preparation": timing(row["preparation"]), "dispatch": timing(row["dispatch"])} for row in rows]},
        "recipient": request.client.igsid, "source_namespace": request.source_namespace,
        "proof": {"recipient": request.client.igsid, "source_namespace": request.source_namespace,
            "request_id": str(request.pk), "request_digest": request.envelope_hmac,
            "decision_id": str(decision.pk), "decision_version": decision.version,
            "decision_digest": decision.envelope_hmac, "task_id": task.pk}}


def render_manager_update(projection, *, copy_version=2):
    """Finite signed WAIT facts, with one complete attributed choice list."""
    from management.services.ig_restock_ack_copy import attributed_label, list_label
    from management.services.ig_fulfillment_delivery import _timing, CUSTOMER_COPY
    try:
        request, decision = projection["current_request"], projection["decision"]
        value = waiting.validate_target(projection["waiting_list"])
        family = request["locale"]
        if family not in {"uk", "ru", "en"} or request["scope"] != "assisted_cart":
            return ""
        rows = decision["lines"]
        if (len(rows) != len(value["items"]) or any(row["availability"] != "wait" for row in rows)
            or [row["line_id"] for row in rows] != [item["line_id"] for item in value["items"]]):
            return ""
        intro, numbered, compact = {
            "uk": ("Менеджер перевірив: ці позиції очікуємо.", "Позиція",
                "Менеджер перевірив усі позиції ({count}): очікуємо. Для точного повідомлення про наявність уточніть коротший список позицій."),
            "ru": ("Менеджер проверил: эти позиции ожидаем.", "Позиция",
                "Менеджер проверил все позиции ({count}): ожидаем. Для точного сообщения о наличии уточните более короткий список позиций."),
            "en": ("Our manager checked: these items are awaiting availability.", "Item",
                "Our manager checked all {count} items: awaiting availability. Please specify a shorter list for an exact availability notification."),
        }[family]
        parts = [intro, attributed_label(family, list_label(value))]
        for index, row in enumerate(rows, 1):
            timings = [_timing(row.get(phase), phase, CUSTOMER_COPY[family]) for phase in ("preparation", "dispatch")]
            timings = [text for text in timings if text]
            if timings:
                parts.append(f"{numbered} {index}: {'; '.join(timings)}.")
        if any(row.get("preparation") or row.get("dispatch") for row in rows):
            parts.append(CUSTOMER_COPY[family]["arrival"])
        text = "\n".join(parts)
        return text if len(text.encode()) <= 950 else compact.format(count=len(rows))
    except (ValueError, KeyError, TypeError, AttributeError):
        return ""


def _row_fingerprint(row):
    """Pure parity of fully loaded rows; deferred fields cannot issue reads."""
    from management.services.ig_revision_authority import _jsonable
    try:
        values = {field.attname: row.__dict__[field.attname] for field in row._meta.concrete_fields}
    except (AttributeError, KeyError) as exc:
        raise ValueError("assisted_update_read_case_row_unavailable") from exc
    return commerce._digest(_jsonable(values))


class HistoricalReadCase:
    """One native readonly graph shared only inside its invitation read pass.

    The native historical reader authenticates every row and source before
    issuance. Consumers recheck their immutable HMAC/task/capture bindings;
    this unit grants no mutable approval, stock, purchase or send authority.
    It is never installed on an ORM object or in a process/global cache.
    """
    def __init__(self, task, request, decision, projection, *, issuer):
        if issuer is not _READ_CASE_ISSUER:
            raise ValueError("assisted_update_native_read_case_required")
        self._issuer, self._active = issuer, True
        self._task, self._request, self._decision = task, request, decision
        self._source = request.source
        self._projection = deepcopy(projection)
        self._rows = (task, request, decision, request.source, request.revision, request.client)
        self._fingerprints = tuple(_row_fingerprint(row) for row in self._rows)
        self._owned_sources, self._settings_fingerprint = {}, None

    def _require(self, task):
        if self._issuer is not _READ_CASE_ISSUER or not self._active:
            raise ValueError("assisted_update_read_case_inactive")
        if (not is_task(task) or _row_fingerprint(task) != self._fingerprints[0]
            or tuple(_row_fingerprint(row) for row in self._rows) != self._fingerprints
            or not commerce._verified(self._request, "request")
            or not commerce._verified(self._decision, "decision")
            or _task_binding(task) != _task_binding(self._task)
            or (task.event_payload or {}) != _payload(self._request, self._decision)):
            raise ValueError("assisted_update_read_case_binding_changed")

    def resolve(self, task):
        self._require(task)
        projected = _projection(task, self._request, self._decision)
        if projected != self._projection:
            raise ValueError("assisted_update_read_case_projection_changed")
        return self._request, self._decision, deepcopy(projected)

    def source_snapshot(self, request):
        self._require(self._task)
        if _row_fingerprint(request) != self._fingerprints[1]:
            raise ValueError("assisted_update_read_case_owner_changed")
        return self._source

    def task_snapshot(self, *, task_id, client_id):
        self._require(self._task)
        if (type(task_id) is not int or type(client_id) is not int
            or task_id != self._task.pk or client_id != self._task.client_id):
            raise ValueError("assisted_update_read_case_task_changed")
        return self._task

    def owned_source(self, revision, source, settings_obj):
        """Share at most invitation/accept/revoke native source checks."""
        from management.services.ig_purpose_sources import owned_source
        self._require(self._task)
        snapshot = _snapshot(self._request)
        revision_id, source_id = getattr(revision, "pk", None), getattr(source, "pk", None)
        client_id = self._request.client_id
        if (type(revision_id) is not int or revision_id <= 0 or type(source_id) is not int or source_id <= 0
            or getattr(revision, "client_id", client_id) != client_id
            or source.client_id != client_id or settings_obj.pk != snapshot["settings_id"]
            or settings_obj.reply_permission_epoch != snapshot["settings_permission_epoch"]):
            raise ValueError("assisted_update_read_case_source_scope_changed")
        settings_fingerprint = _row_fingerprint(settings_obj)
        if self._settings_fingerprint is not None and settings_fingerprint != self._settings_fingerprint:
            raise ValueError("assisted_update_read_case_settings_changed")
        self._settings_fingerprint = settings_fingerprint
        key = (revision_id, source_id, settings_obj.pk, settings_obj.reply_permission_epoch)
        source_fingerprint = _row_fingerprint(source)
        if key not in self._owned_sources:
            if len(self._owned_sources) >= 3:
                raise ValueError("assisted_update_read_case_source_bound")
            owned, reason = owned_source(revision, source, settings_obj, require_head=False)
            if not owned:
                return None, reason
            original_revision, original_source, proof = owned
            if (original_revision.client_id != client_id or original_source.client_id != client_id
                or original_source.sender_id != self._request.client.igsid
                or original_revision.pk != revision_id or original_source.pk != source_id
                or _row_fingerprint(original_source) != source_fingerprint):
                raise ValueError("assisted_update_read_case_source_changed")
            rows = (original_revision, original_revision.client, original_source)
            self._owned_sources[key] = (rows, tuple(_row_fingerprint(row) for row in rows), deepcopy(proof), reason)
        rows, fingerprints, proof, reason = self._owned_sources[key]
        if (tuple(_row_fingerprint(row) for row in rows) != fingerprints
            or _row_fingerprint(rows[2]) != source_fingerprint):
            raise ValueError("assisted_update_read_case_source_changed")
        return (rows[0], rows[2], deepcopy(proof)), reason

    def close(self):
        self._active = False
        self._task = self._request = self._decision = self._source = None
        self._rows, self._fingerprints, self._projection = (), (), None
        self._owned_sources.clear()
        self._settings_fingerprint = None


def capture_read_case(task):
    """Issue one original native graph; callers must close it in finally."""
    request, decision = _historical_rows(task)
    projected = _projection(task, request, decision)
    return HistoricalReadCase(task, request, decision, projected, issuer=_READ_CASE_ISSUER)


def _resolve_read_case(task, read_case):
    if not isinstance(read_case, HistoricalReadCase):
        raise ValueError("assisted_update_native_read_case_required")
    return read_case.resolve(task)


def case(task, projection, *, read_case=None):
    if read_case is None:
        request, decision = _historical_rows(task)
        original = _projection(task, request, decision)
    else:
        request, decision, original = _resolve_read_case(task, read_case)
    if (not isinstance(projection, dict) or projection.get("current_request") != original["current_request"]
        or projection.get("decision") != original["decision"]):
        raise ValueError("assisted_update_projection_changed")
    return request, decision


def original_baseline(task):
    request, decision = _historical_rows(task)
    return deepcopy(_capture(task, request, decision)["baseline"])


def frozen_target(task):
    request, decision = _historical_rows(task)
    return deepcopy(_capture(task, request, decision)["target"])


def target(request, decision, *, cart, now):
    """Pinned whole-list target; no consumed scope or latest-row substitution."""
    _eligible(request, decision)
    coverage = scopes.intention_coverage(request.client_id, request.intention_id, now=now)
    if not coverage.get("ready"):
        raise ValueError(coverage.get("reason") or "assisted_update_intention_unverified")
    if any(coverage[key] for key in ("settled_line_ids", "inflight_line_ids", "cancelled_line_ids")):
        raise ValueError("assisted_update_consumed_intention")
    items = waiting._assisted_items(request.client, cart, now=now, request=request, decision=decision)
    value = {"schema": waiting.SCHEMA, "completion": "all", "items": items or []}
    if value != _original_target(request, decision) or len(value["items"]) != len(cart["lines"]):
        raise ValueError("assisted_update_full_scope_changed")
    waiting.verify_item_sources(value, request.client, request.source_namespace)
    return value


def _baseline(value, *, now):
    baseline = {item["line_id"]: waiting.observe_item(item, now=now)["configuration_digest"] for item in value["items"]}
    if any(not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) for digest in baseline.values()):
        raise ValueError("assisted_update_configuration_unverified")
    return baseline


def _current(request, decision, settings_obj, *, now):
    from management.services.ig_commerce_projection import capture_current_selection_lines
    from management.services.ig_purpose_sources import owned_source
    source = _snapshot(request)
    if settings_obj.pk != source["settings_id"]:
        raise ValueError("settings_changed")
    commerce._permission(request.client, settings_obj.pk,
        expected_settings_epoch=source["settings_permission_epoch"],
        expected_client_epoch=source["client_permission_epoch"])
    commerce._actor(decision.actor_id)
    if not decision.actor.is_active:
        raise ValueError("actor_forbidden")
    Request, Decision, _Execution, _Image = commerce._models()
    if (Request.objects.filter(client_id=request.client_id, intention_id=request.intention_id).order_by("-version").values_list("pk", flat=True).first() != request.pk
        or Decision.objects.filter(request=request).order_by("-version").values_list("pk", flat=True).first() != decision.pk
        or commerce.latest_assisted_request(request.client_id).pk != request.pk):
        raise ValueError("assisted_update_manager_head_changed")
    if (request.created_at > now or decision.created_at > now or not decision.expires_at
        or now >= decision.expires_at or now >= commerce._date(source["expires_at"])
        or decision.expires_at > commerce._date(source["expires_at"])):
        raise ValueError("manager_review_expired")
    owned, reason = owned_source(request.revision, request.source, settings_obj, require_head=False)
    if not owned or owned[2] not in source["sources"]:
        raise ValueError(reason or "assisted_update_source_changed")
    cart = capture_current_selection_lines(request.client_id, now=now)
    if (cart.get("status") != "captured" or not cart.get("coverage_complete")
        or commerce._configuration_capture(cart) != commerce._configuration_capture(source["full_capture"])):
        raise ValueError("assisted_update_full_scope_changed")
    if commerce._business_owner(request.client, cart["scope"]) != source["owner_scope"]:
        raise ValueError("assisted_update_commercial_owner_changed")
    return cart, target(request, decision, cart=cart, now=now)


def validated_update_task(task, settings_obj, *, now):
    try:
        request, decision = _historical_rows(task)
        frozen = _capture(task, request, decision)
        _cart, value = _current(request, decision, settings_obj, now=now)
        if value != frozen["target"] or _baseline(value, now=now) != frozen["baseline"]:
            raise ValueError("assisted_update_configuration_changed")
        return _projection(task, request, decision)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        return {"ready": False, "reason": str(exc) if isinstance(exc, ValueError) else "assisted_update_unverified"}


def validated_update_history(task, *, read_case=None):
    """Immutable source and task binding, independent of today's cart/head/TTL."""
    try:
        if read_case is None:
            return _projection(task, *_historical_rows(task))
        return _resolve_read_case(task, read_case)[2]
    except (ValueError, KeyError, TypeError, AttributeError):
        return {"ready": False, "reason": "delivery_binding_invalid"}


def create_approved_task(request, decision, *, now):
    """Called only for a newly inserted approval inside its existing transaction.

    Retrying the approval operation never enters this hook, so no historical
    approval is backfilled. Ineligible or incomplete WAIT terms remain manager
    evidence and cannot create a misleading customer offer.
    """
    from management.models import IgFollowUpTask, InstagramBotSettings
    if not transaction.get_connection().in_atomic_block:
        raise ValueError("assisted_update_requires_approval_transaction")
    # auto_now_add runs after the caller's boundary timestamp. Admission uses
    # the real newly inserted approval time without extending its sealed TTL.
    now = max(now, decision.created_at)
    try:
        _eligible(request, decision)
        commerce.verified_assisted_decision_from_evidence(request, decision, historical=True)
        scopes._source_history(request)
        settings_obj = InstagramBotSettings.objects.get(pk=_snapshot(request)["settings_id"])
        cart, value = _current(request, decision, settings_obj, now=now)
        baseline = _baseline(value, now=now)
        # Resolve again after catalog observations. A same-PK edit during
        # capture cannot freeze its new graph beneath the old source label.
        second_cart, second_target = _current(request, decision, settings_obj, now=now)
        if second_cart != cart or second_target != value or _baseline(value, now=now) != baseline:
            return None
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    task, created = IgFollowUpTask.objects.get_or_create(
        event_key=f"ig-assisted-manager-update:{request.pk}:{decision.version}", defaults={
            "client_id": request.client_id, "due_at": now, "kind": "fulfillment", "reason": ORIGIN,
            "trigger": "event", "event_occurred_at": now, "policy_version": POLICY_VERSION,
            "event_payload": _payload(request, decision)})
    if not created:
        case(task, _projection(task, request, decision))
        return task
    family, language = locale(request)
    body = {"schema": SOURCE_KIND, "binding": _task_binding(task), "target": value, "baseline": baseline,
        "configuration": commerce._configuration_capture(cart), "locale": family, "reply_language": language}
    key_id = next(iter(commerce._keys()))
    task.manager_context = {**(task.manager_context or {}), CONTEXT_KEY: {
        "body": body, "key_id": key_id, "hmac": commerce._sign("manager_update", body, key_id)}}
    _capture(task, request, decision)
    task.save(update_fields=["manager_context", "updated_at"])
    return task
