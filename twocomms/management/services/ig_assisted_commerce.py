"""Source-owned assisted requests and append-only exact manager terms.
It does not create a Product, inventory count/hold, proposal, invoice, Order,
customer send, provider request or owned image. Those existing effect owners
must re-read this evidence under their own transaction and source fences.

The typed reader is current approval evidence, not a payable execution grant.
Catalog rows still need independent ordinary catalog configuration and pricing;
custom READY capacity is exactly the approved quantity, never checked surplus.
Customer purchase/partial-choice adoption and one-shot Execution remain a
separate producer. A question, a manager draft and a generic case approval
cannot supply any of those missing authorities.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone as datetime_timezone
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import json
import re
import uuid

from django.db import transaction
from django.utils import timezone

from management.services import ig_fulfillment_reviews as review_evidence

PURPOSE = "assisted-commerce.v1"
REQUEST_SCHEMA = "ig-assisted-commerce-request.v1"
DECISION_SCHEMA = "ig-assisted-commerce-decision.v1"
MAX_LINES = 16  # Parent intention remains complete; execution has its own cap.
MAX_SOURCES = 16
MAX_QUESTIONS = 31  # Existing inquiry producer truncates at 32; fail at that edge.
MAX_QUANTITY = 50
MAX_VALUE = Decimal("1000000.00")
REQUEST_TTL = timedelta(hours=24)
APPROVAL_TTL = timedelta(hours=24)
MAX_JSON_BYTES = 262144
_ISSUER = object()
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_CONTROLS = re.compile(r"[\x00-\x1f\x7f]")
_URL = re.compile(r"(?:https?://|(?:project|library)-file:|file://|data:)", re.I)


class AssistedDecisionError(ValueError):
    def __init__(self, reason):
        self.reason = str(reason)[:128]
        super().__init__(self.reason)


def _fail(reason):
    raise AssistedDecisionError(reason)


def _models():
    from management.ig_assisted_commerce_models import (
        IgAssistedCommerceRequest, IgAssistedCommerceDecision,
        IgAssistedCommerceExecution, IgAssistedCommerceImage,
    )
    return IgAssistedCommerceRequest, IgAssistedCommerceDecision, IgAssistedCommerceExecution, IgAssistedCommerceImage


def _json(value):
    try:
        result = json.dumps(value, sort_keys=True, ensure_ascii=False,
            separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError):
        _fail("snapshot_not_finite_json")
    if len(result.encode()) > MAX_JSON_BYTES:
        _fail("snapshot_bound")
    return result


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _hash(value):
    return isinstance(value, str) and bool(_HASH.fullmatch(value))


def _positive(value):
    return type(value) is int and value > 0


def _uuid(value, reason="identity_invalid"):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        _fail(reason)


def _text(value, *, limit, required=False, reason="text_invalid"):
    if not isinstance(value, str) or len(value) > limit or _CONTROLS.search(value) or _URL.search(value):
        _fail(reason)
    if required and not value.strip():
        _fail(reason)
    return value.strip()


def _now(value=None):
    value = value or timezone.now()
    if not isinstance(value, datetime) or timezone.is_naive(value):
        _fail("clock_invalid")
    return value.astimezone(datetime_timezone.utc)


def _date(value, reason="timestamp_invalid"):
    try:
        at = datetime.fromisoformat(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        _fail(reason)
    if not isinstance(at, datetime) or timezone.is_naive(at):
        _fail(reason)
    return at.astimezone(datetime_timezone.utc)


def _price(value, *, required=False):
    # No rounding, bool coercion, numeric floats, scientific notation or NaN.
    if value is None and not required:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9]\d{0,6})(?:\.\d{1,2})?", value):
        _fail("unit_price_invalid")
    try:
        amount = Decimal(value)
    except InvalidOperation:
        _fail("unit_price_invalid")
    if not amount.is_finite() or not 0 < amount <= MAX_VALUE:
        _fail("unit_price_invalid")
    return amount.quantize(Decimal("0.01"))


def _timing(value):
    if value is None:
        return None  # WAIT with no estimate remains unknown.
    if (not isinstance(value, dict) or set(value) != {"min_days", "max_days", "unit"}
        or type(value["min_days"]) is not int or type(value["max_days"]) is not int
        or not 0 <= value["min_days"] <= value["max_days"] <= 90
        or value["unit"] not in {"calendar", "business"}):
        _fail("timing_invalid")
    return deepcopy(value)


@dataclass(frozen=True)
class AssistedDecisionLine:
    line_id: str
    recipient_id: str
    original_index: int
    item_kind: str
    product_id: int | None
    color_variant_id: int | None
    title: str
    color: str
    size: str
    fit_code: str
    quantity: int
    unit_price: Decimal | None
    availability: str
    checked_quantity: int | None
    supply_basis: str | None
    source_location: str
    image_id: uuid.UUID | None
    image_digest: str
    _options_json: str = "{}"
    _preparation_json: str = "null"
    _dispatch_json: str = "null"
    _answers_json: str = "[]"

    @property
    def ready_capacity(self):
        return self.quantity if self.availability == "ready" else 0

    @property
    def ready_total(self):
        return self.unit_price * self.quantity if self.availability == "ready" else Decimal("0.00")

    @property
    def line_digest(self):
        return _digest(self.as_dict())

    def as_dict(self):
        return {"line_id": self.line_id, "recipient_id": self.recipient_id, "original_index": self.original_index,
            "item_kind": self.item_kind, "product_id": self.product_id, "color_variant_id": self.color_variant_id,
            "title": self.title, "color": self.color, "size": self.size, "fit_code": self.fit_code,
            "quantity": self.quantity, "unit_price": format(self.unit_price, ".2f") if self.unit_price is not None else None,
            "currency": "UAH", "availability": self.availability, "checked_quantity": self.checked_quantity,
            "supply_basis": self.supply_basis, "source_location": self.source_location,
            "image_id": str(self.image_id) if self.image_id else None, "image_digest": self.image_digest,
            "option_values": json.loads(self._options_json), "preparation": json.loads(self._preparation_json),
            "dispatch": json.loads(self._dispatch_json), "question_answers": json.loads(self._answers_json)}


def normalize_assisted_lines(raw, *, source_lines, questions=(), require_ready_answers=False):
    """Pure exact full-line normalizer. Its dictionaries convey no authority.

    Callers pass source_lines/questions only after re-reading sealed source
    request evidence. Every identity/index is immutable; edited terms are a
    manager offer, never a retroactive rewrite of the customer's wishes.
    READY is physical; the eventual execution also needs price/catalog proof,
    customer adoption, source/current-head and finite-consumption checks.
    """
    if (not isinstance(raw, (list, tuple)) or not isinstance(source_lines, (list, tuple))
        or not 1 <= len(source_lines) <= MAX_LINES or len(raw) != len(source_lines)
        or any(not isinstance(row, dict) for row in (*raw, *source_lines))):
        _fail("full_line_coverage_invalid")
    _json({"items": raw, "source_lines": source_lines, "questions": questions})
    if (not isinstance(questions, (list, tuple)) or len(questions) > MAX_QUESTIONS
        or any(not isinstance(question, dict) or not _hash(question.get("question_id"))
            or not isinstance(question.get("line_id"), str) for question in questions)
        or len({question["question_id"] for question in questions}) != len(questions)):
        _fail("question_coverage_invalid")
    for source in source_lines:
        _text(source.get("line_id"), limit=128, required=True)
        _text(source.get("recipient_id"), limit=128, required=True)
        if type(source.get("original_index")) is not int or source["original_index"] < 0:
            _fail("line_identity_changed")
    identities = [(row.get("line_id"), row.get("recipient_id"), row.get("original_index")) for row in source_lines]
    if len(set(value[0] for value in identities)) != len(identities):
        _fail("full_line_coverage_invalid")
    allowed = {"line_id", "recipient_id", "original_index", "item_kind", "product_id", "color_variant_id",
        "title", "color", "size", "fit_code", "quantity", "unit_price", "currency", "availability",
        "checked_quantity", "supply_basis", "source_location", "option_values", "preparation", "dispatch",
        "image_id", "image_digest", "question_answers"}
    result = []
    for index, (row, source) in enumerate(zip(raw, source_lines, strict=True)):
        if set(row) - allowed or any(row.get(key) != source.get(key) or type(row.get(key)) is not type(source.get(key))
            for key in ("line_id", "recipient_id", "original_index")) or row["original_index"] != index:
            _fail("line_identity_changed")
        if type(row["original_index"]) is not int:
            _fail("line_identity_changed")
        line_id = _text(row["line_id"], limit=128, required=True)
        recipient = _text(row["recipient_id"], limit=128, required=True)
        kind = row.get("item_kind")
        if kind not in {"catalog_apparel", "custom_apparel"}:
            _fail("item_kind_invalid")
        product, variant = row.get("product_id"), row.get("color_variant_id")
        if kind == "custom_apparel" and (product is not None or variant is not None):
            _fail("custom_catalog_identity_invalid")
        if kind == "custom_apparel" and source.get("catalog_item_spec"):
            _fail("resolved_catalog_cannot_be_custom")
        if kind == "catalog_apparel" and (not _positive(product) or not _positive(variant)
            or product != (source.get("source_choices") or {}).get("product_id")):
            _fail("catalog_identity_not_source_owned")
        quantity = row.get("quantity")
        if type(quantity) is not int or not 1 <= quantity <= MAX_QUANTITY:
            _fail("quantity_invalid")
        state = row.get("availability")
        if state not in {"ready", "wait", "unavailable", "clarify"} or row.get("currency") != "UAH":
            _fail("availability_or_currency_invalid")
        ready = state == "ready"
        price = _price(row.get("unit_price"), required=ready)
        title = _text(row.get("title", ""), limit=200, required=ready, reason="title_invalid")
        color = _text(row.get("color", ""), limit=100, required=ready, reason="color_invalid")
        size = _text(row.get("size", ""), limit=16, required=ready, reason="size_invalid")
        fit = _text(row.get("fit_code", ""), limit=50, reason="fit_code_invalid")
        checked, basis = row.get("checked_quantity"), row.get("supply_basis")
        if checked is not None and (type(checked) is not int or not 0 <= checked <= 999):
            _fail("checked_quantity_invalid")
        if basis not in {None, "warehouse_checked", "accessible_unrecorded", "incoming_confirmed"}:
            _fail("supply_basis_invalid")
        if ready and (type(checked) is not int or checked < quantity or basis not in {"warehouse_checked", "accessible_unrecorded"}):
            _fail("physical_ready_not_checked")
        if state == "wait" and basis not in {None, "incoming_confirmed"}:
            _fail("wait_physical_basis_invalid")
        options = row.get("option_values", {})
        if not isinstance(options, dict) or len(options) > 12:
            _fail("options_invalid")
        normalized_options = {_text(key, limit=100, required=True): _text(value, limit=100, required=True)
            for key, value in options.items()}
        if len(normalized_options) != len(options):
            _fail("options_identity_collision")
        options = normalized_options
        if kind == "custom_apparel" and options:
            _fail("custom_generic_options_forbidden")
        image_id, image_digest = row.get("image_id"), row.get("image_digest", "")
        if image_id is not None:
            image_id = _uuid(image_id, "image_identity_invalid")
            if not _hash(image_digest):
                _fail("image_identity_invalid")
        elif image_digest:
            _fail("image_identity_invalid")
        if kind == "catalog_apparel":
            spec = source.get("catalog_item_spec") or {}
            canonical = {"product_id": spec.get("product_id"), "color_variant_id": spec.get("color_variant_id"),
                "title": spec.get("title"), "color": spec.get("color"), "size": spec.get("size"),
                "fit_code": spec.get("fit_option_code"), "quantity": spec.get("qty"),
                "option_values": spec.get("option_values"), "unit_price": spec.get("unit_price")}
            actual = {"product_id": product, "color_variant_id": variant, "title": title, "color": color,
                "size": size, "fit_code": fit, "quantity": quantity, "option_values": options,
                "unit_price": format(price, ".2f") if price is not None else None}
            if not spec or actual != canonical or image_id is not None or image_digest:
                _fail("catalog_terms_changed")
        expected_questions = {question["question_id"]: question for question in questions
            if question.get("line_id") == line_id}
        answers = row.get("question_answers", [])
        if (not isinstance(answers, list) or len(answers) > MAX_QUESTIONS or any(not isinstance(answer, dict)
                or set(answer) != {"question_id", "state", "answer"}
                or not _hash(answer.get("question_id")) for answer in answers)
            or len({answer["question_id"] for answer in answers}) != len(answers)
            or {answer["question_id"] for answer in answers} != set(expected_questions)):
            _fail("question_answer_coverage_invalid")
        cleaned = []
        for answer in answers:
            if answer["state"] not in {"confirmed", "unknown"}:
                _fail("question_answer_state_invalid")
            cleaned.append({"question_id": answer["question_id"], "state": answer["state"],
                "answer": _text(answer["answer"], limit=500,
                    required=answer["state"] == "confirmed", reason="question_answer_invalid")})
        if require_ready_answers and ready and any(answer["state"] != "confirmed" for answer in cleaned):
            _fail("ready_question_unanswered")
        result.append(AssistedDecisionLine(line_id, recipient, index, kind, product, variant,
            title, color, size, fit, quantity, price, state, checked, basis,
            _text(row.get("source_location", ""), limit=350), image_id, image_digest,
            _json(options), _json(_timing(row.get("preparation"))), _json(_timing(row.get("dispatch"))), _json(cleaned)))
    # Larger intentions can be reviewed fully, but a READY manager approval
    # cannot strand a basket beyond the established ordinary checkout bounds.
    if sum(line.ready_capacity for line in result) > MAX_QUANTITY:
        _fail("ready_quantity_requires_manager_split_review")
    if sum((line.ready_total for line in result), Decimal("0.00")) > MAX_VALUE:
        _fail("ready_value_requires_manager_split_review")
    if require_ready_answers and any(not question.get("line_id") for question in questions):
        _fail("question_identity_unresolved")
    return tuple(result)


_keys = review_evidence._keys


def _columns(row):
    result = review_evidence._columns(row)
    mutable = getattr(row, "MUTABLE_FIELDS", ())
    for column in row._meta.concrete_fields:
        if column.name in mutable:
            result.pop(column.attname, None)
        elif column.attname in result and isinstance((value := getattr(row, column.attname)), datetime):
            result[column.attname] = value.astimezone(datetime_timezone.utc).isoformat(timespec="microseconds")
    return result


def _canonical_catalog_observations(specs):
    """Ordinary full catalog-subset quantity pricing and native descriptors.

    This is an operator observation, not an inventory/quote capability. It
    uses the same brigade/bundle pricing producer as ordinary checkout, with
    the same catalog line order and quantities across distinct recipients.
    Custom rows neither qualify for nor increase catalog quantity discounts.
    """
    if not specs:
        return {}
    from storefront.models import Product, ProductStatus
    from productcolors.models import ProductColorVariant
    from management.services.ig_catalog_pricing import prepare_pricing_context
    from storefront.services.brigade_commerce import calculate_brigade_cart_pricing
    rows = list(specs.items())
    products = {row.pk: row for row in Product.objects.filter(pk__in={spec["product_id"] for _, spec in rows},
        status=ProductStatus.PUBLISHED).select_related("category", "catalog", "size_grid")}
    variants = {row.pk: row for row in ProductColorVariant.objects.filter(
        pk__in={spec["color_variant_id"] for _, spec in rows}).select_related("color")}
    if (len(products) != len({spec["product_id"] for _, spec in rows})
        or len(variants) != len({spec["color_variant_id"] for _, spec in rows})
        or any(variants[spec["color_variant_id"]].product_id != spec["product_id"] for _, spec in rows)):
        _fail("catalog_observation_identity_changed")
    prepare_pricing_context(list(products.values()), list(variants.values()))
    pricing = calculate_brigade_cart_pricing({str(index): deepcopy(spec) for index, (_line_id, spec) in enumerate(rows)},
        products, variants)
    result = {}
    for index, (line_id, spec) in enumerate(rows):
        priced = pricing.lines.get(str(index))
        if not priced or priced.unit_price * spec["qty"] != priced.line_total:
            _fail("catalog_tier_requires_manager_review")
        product, variant = products[spec["product_id"]], variants[spec["color_variant_id"]]
        title = _text(str(product.title or ""), limit=200, required=True, reason="catalog_title_requires_editor")
        color = _text(str(variant.color.name or ""), limit=100, required=True, reason="catalog_color_requires_editor")
        if title != product.title or color != variant.color.name:
            _fail("catalog_descriptor_requires_editor")
        result[line_id] = {**deepcopy(spec), "title": title, "color": color,
            "unit_price": format(priced.unit_price, ".2f")}
    return result


def _sign(kind, envelope, key_id):
    key = _keys().get(key_id)
    return hmac.new(key.encode(), (PURPOSE + ":" + kind + ":" + _json(envelope)).encode(), hashlib.sha256).hexdigest() if key else ""


def _seal(row, kind, snapshot, *, operation_input_digest):
    # Capture and storage share exactly the bounded finite JSON wire shape.
    # In particular dataclass tuples cannot drift to lists only after reload.
    snapshot = json.loads(_json(snapshot))
    row.key_id = next(iter(_keys()))
    row.snapshot_digest = _digest(snapshot)
    row.envelope = {"version": 1, "purpose": PURPOSE, "kind": kind, "columns": _columns(row),
        "snapshot": deepcopy(snapshot), "operation_input_digest": operation_input_digest}
    row.envelope_hmac = _sign(kind, row.envelope, row.key_id)
    row.save(force_insert=True)
    return row


def _verified(row, kind):
    if not row or not isinstance(row.envelope, dict):
        return False
    try:
        expected = _sign(kind, row.envelope, row.key_id)
        return bool(expected and hmac.compare_digest(expected, row.envelope_hmac)
            and row.envelope.get("version") == 1 and row.envelope.get("purpose") == PURPOSE
            and row.envelope.get("kind") == kind and row.envelope.get("columns") == _columns(row)
            and _digest(row.envelope.get("snapshot")) == row.snapshot_digest)
    except AssistedDecisionError:
        return False


def _actor(actor_id):
    from django.contrib.auth import get_user_model
    from management.bot_access import has_all_bot_capabilities, OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION
    # Do not accept an object's cached permissions, or a claimed actor ID from
    # a provider. The authenticated endpoint supplies actor.pk separately.
    actor = get_user_model().objects.filter(pk=actor_id).first()
    if not actor or not actor.is_staff or not has_all_bot_capabilities(actor,
            OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION):
        _fail("actor_forbidden")
    return actor


def _permission(client, settings_id, *, expected_settings_epoch=None, expected_client_epoch=None):
    from types import SimpleNamespace
    from management.models import IgClient, InstagramBotSettings
    from management.services.instagram_bot import ingress_provider_namespace
    # This is an operator business boundary. Bot pause/takeover/global enable
    # and customer-send eligibility belong to the existing bot effect owner.
    # Durable epoch changes, source ownership and privacy remain mandatory.
    if not client:
        _fail("client_unavailable")
    fresh = IgClient.objects.filter(pk=client.pk).first()
    settings_obj = InstagramBotSettings.objects.filter(pk=settings_id).first()
    if not fresh or fresh.privacy_erasure_started_at or fresh.hidden_at:
        _fail("client_unavailable")
    if not settings_obj or not ingress_provider_namespace(settings_obj):
        _fail("source_namespace_unknown")
    current = SimpleNamespace(settings_epoch=settings_obj.reply_permission_epoch,
        client_epoch=fresh.reply_permission_epoch)
    if (expected_settings_epoch is not None and current.settings_epoch != expected_settings_epoch
        or expected_client_epoch is not None and current.client_epoch != expected_client_epoch):
        _fail("permission_epoch_changed")
    return settings_obj, current


def _configuration_capture(capture):
    """Exact owned configuration, excluding only separately proved purchase."""
    return {"scope": deepcopy(capture.get("scope")), "session_id": capture.get("session_id"),
        "generation": capture.get("generation"), "active_line_id": capture.get("active_line_id"),
        "lines": [{"line_id": row["line_id"], "recipient_id": row["recipient_id"], "index": row["index"],
            "fields": {key: deepcopy(value) for key, value in (row.get("fields") or {}).items()
                if key != "purchase_requested"}, "defaults": deepcopy(row.get("defaults"))}
            for row in capture["lines"]]}


def _purchase_extension(original, current, source_id, client, settings_obj, now):
    """Permit only a real later canonical purchase with unchanged full choices."""
    from management.models import InstagramBotMessage
    from management.services.ig_commerce_turns import parse_turn
    from management.services.ig_fulfillment_requirements import extract_requirements
    from management.services.ig_product_inquiries import capture_product_inquiries
    from management.services.ig_response_cart_plan import source_line_preferences
    if _configuration_capture(original) != _configuration_capture(current):
        return False
    watermark = (current.get("source_watermark") or {}).get("message_id")
    if type(watermark) is not int or watermark <= source_id:
        return False
    rows = list(InstagramBotMessage.objects.filter(client=client, role="user", sender_id=client.igsid,
        source="webhook", provider_namespace=settings_obj and current["scope"]["source_namespace"],
        pk__gt=source_id, pk__lte=watermark).exclude(status="failed")
        .select_related("commerce_turn_decision").order_by("pk")[:MAX_SOURCES + 1])
    if not 1 <= len(rows) <= MAX_SOURCES or rows[-1].pk != watermark:
        return False
    for row in rows:
        parsed = parse_turn(row.text)
        hint = extract_requirements(row.text, provider_timestamp=row.provider_created_at, now=now)
        if (not row.mid or not row.provider_created_at or row.provider_created_at > now
            or row.pk not in current["fence"]["source_ids"] or not getattr(row, "commerce_turn_decision", None)
            or parsed.purchase_requested is not True or parsed.field_updates or parsed.preference_withdrawals
            or parsed.exact_product_id or parsed.garment_type or parsed.line_operations or parsed.pending_clarification
            or parsed.info_topics or parsed.reset_requested or parsed.new_order_requested or parsed.exchange_requested
            or parsed.custom_print_requested or parsed.personalized_fit_requested
            or hint.get("request_detected") or hint.get("custom_print_hint")
            or hint.get("supersession_hint") in {"changed", "withdrawn"} or review_evidence._context_requirements([hint])):
            return False
    questions = capture_product_inquiries([{"message_id": row.pk, "role": "user", "text": row.text,
        "source_digest": hashlib.sha256(row.text.encode()).hexdigest()} for row in rows], cart_capture=current)
    if questions.get("gap") or questions.get("inquiries"):
        return False
    return all((preferences := source_line_preferences(current, line)).get("values", {}).get("purchase_requested") is True
        and preferences.get("evidence", {}).get("purchase_requested", {}).get("source_message_id") in {row.pk for row in rows}
        for line in current["lines"])


def _business_owner(client, scope):
    """Use the identical fresh owner normalization at both capture fences."""
    from management.models import IgClient
    from management.services.ig_revision_checkout import checkout_owner_scope
    if scope.get("order_id") is not None:
        _fail("commercial_owner_unavailable")
    fresh = IgClient.objects.filter(pk=client.pk).first()
    if not fresh or fresh.privacy_erasure_started_at or fresh.hidden_at:
        _fail("client_unavailable")
    owner = checkout_owner_scope(fresh)
    if scope.get("episode_id") is None and fresh.current_commercial_episode_id is None:
        return {"client": {"pk": fresh.pk, "reply_permission_epoch": fresh.reply_permission_epoch},
            "episode": None, "deal": None}
    if not owner or owner["episode"]["pk"] != scope.get("episode_id"):
        _fail("commercial_owner_unavailable")
    # These two bot execution controls do not amend human-reviewed business
    # terms. Epoch/privacy and every actual deal/episode owner field remain
    # exact; customer_send_boundary separately enforces the live bot controls.
    owner["client"] = {key: value for key, value in owner["client"].items()
        if key not in {"bot_paused", "manager_takeover"}}
    return owner


def _catalog_configuration_observations(cart, catalog):
    """Known declared SKU/price hints, without claiming stock or checkout truth.

    The ordinary catalog resolver deliberately omits zero-stock size specs.
    This operator observation ignores only that count: a published product,
    unique source-owned colour, enabled declared exact size rule, enabled
    options/fit, compatible size contract and unique current price still apply.
    The eventual quote independently revalidates all ordinary catalog facts.
    """
    from storefront.models import Product, ProductStatus
    from productcolors.models import ProductColorVariant
    from product_catalog.models import VariantSizeRule
    from management.services.ig_source_cart_catalog import _color_matches
    from management.services.ig_catalog_pricing import prepare_pricing_context, resolve_product_pricing
    from management.services.ig_response_cart_plan import source_line_preferences
    result = deepcopy(catalog.get("item_specs_by_line") or {})
    for row in cart["lines"]:
        if row["line_id"] in result:
            continue
        preferences = source_line_preferences(cart, row)
        values = preferences.get("values", {}) if preferences else {}
        product_id = values.get("product_id")
        size, fit = str(values.get("size") or "").upper(), str(values.get("fit_option_code") or "").lower()
        quantity = values.get("quantity")
        if quantity is None and row.get("defaults", {}).get("quantity") == {
                "value": 1, "authority": "existing_cart_default", "source_confirmed": False}:
            quantity = 1
        if not _positive(product_id) or not size or not values.get("color") or not _positive(quantity):
            continue
        product = Product.objects.filter(pk=product_id, status=ProductStatus.PUBLISHED).select_related(
            "category", "catalog", "size_grid").first()
        if not product:
            continue
        variants = list(ProductColorVariant.objects.filter(product=product).select_related("color").order_by("pk")[:33])
        if len(variants) > 32:
            _fail("catalog_observation_variant_bound")
        matches = [variant for variant in variants if _color_matches(values["color"], variant)]
        if len(matches) != 1:
            continue
        variant = matches[0]
        rules = list(VariantSizeRule.objects.filter(variant=variant, size__iexact=size,
            fit_code__in=["", fit]).order_by("pk")[:3])
        specific = [rule for rule in rules if rule.fit_code == fit]
        general = [rule for rule in rules if not rule.fit_code]
        if len(rules) > 2 or len(specific) > 1 or len(general) > 1:
            continue
        rule = specific[0] if specific else general[0] if general else None
        if not rule or not rule.is_enabled:
            continue
        prepare_pricing_context([product], [variant])
        pricing = resolve_product_pricing(product, variants=[variant], selected_variant_id=variant.pk,
            option_values={"fit": fit} if fit else {}, context_prepared=True)
        configs = [config for config in pricing["configurations"] if config.get("fit_code") == fit
            and config.get("has_compatible_size_contract") and size in config.get("compatible_sizes", ())]
        # A multi-choice non-fit axis is still unresolved, even at equal prices.
        choices = {_json(config.get("option_values") or {}) for config in configs}
        prices = {config["price"] for config in configs}
        if len(choices) != 1 or len(prices) != 1:
            continue
        result[row["line_id"]] = {"line_id": row["line_id"], "recipient_id": row["recipient_id"],
            "product_id": product_id, "size": size, "fit_option_code": fit, "qty": quantity,
            "color_variant_id": variant.pk, "option_values": json.loads(next(iter(choices))),
            "unit_price": str(next(iter(prices)))}
    return _canonical_catalog_observations({row["line_id"]: result[row["line_id"]]
        for row in cart["lines"] if row["line_id"] in result})


def _capture(client, revision_id, settings_id, now, *, expected_revision_digest=None, prior_snapshot=None):
    """Only this native producer reads source/cart/owner facts, never a raw DTO."""
    from management.models import IgCustomerTurnRevision, InstagramBotMessage, IgCommerceSelectionSession
    from management.services.ig_purpose_sources import owned_source
    from management.services.ig_commerce_projection import capture_current_selection_lines
    from management.services.ig_revision_checkout import checkout_owner_scope, lock_checkout_cart_sources
    from management.services.ig_revision_cart_binding import stable_checkout_source_capture
    from management.services.ig_product_inquiries import capture_product_inquiries
    from management.services.ig_source_cart_catalog import resolve_source_cart_catalog
    from management.services.ig_response_cart_plan import source_line_preferences
    from management.services.ig_reply_language import resolve_own_source_reply_language, own_reply_text
    settings_obj, permission = _permission(client, settings_id)
    revision = IgCustomerTurnRevision.objects.select_for_update().filter(pk=revision_id, client=client).first()
    if not revision or (expected_revision_digest is not None and revision.snapshot_digest != expected_revision_digest):
        _fail("source_revision_changed")
    frozen = list(revision.sources.select_related("message").order_by("ordinal")[:MAX_SOURCES + 1])
    if not 1 <= len(frozen) <= MAX_SOURCES:
        _fail("source_coverage_bound")
    owned, source_payloads = [], []
    for member in frozen:
        value, reason = owned_source(revision, member.message, settings_obj, require_head=prior_snapshot is None)
        if not value:
            _fail(reason)
        _revision, source, proof = value
        if source.provider_created_at > now:
            _fail("source_event_in_future")
        owned.append(proof)
        payload = next((row for row in revision.bundle_snapshot["sources"] if row.get("message_id") == source.pk), None)
        if not payload or payload.get("source_digest") != proof["digest"]:
            _fail("source_snapshot_changed")
        source_payloads.append(payload)
    latest_id = max(proof["message_id"] for proof in owned)
    # Lock the current revision sources too; not every question changes a cart.
    ids = sorted(proof["message_id"] for proof in owned)
    if list(InstagramBotMessage.objects.select_for_update().filter(client=client, pk__in=ids)
            .order_by("pk").values_list("pk", flat=True)) != ids:
        _fail("source_scope_changed")
    cart = capture_current_selection_lines(client.pk, now=now)
    if (cart.get("status") != "captured" or not cart.get("coverage_complete")
        or not 1 <= len(cart.get("lines") or []) <= MAX_LINES or stable_checkout_source_capture(cart) is None
        or not lock_checkout_cart_sources(client, cart)):
        _fail("needs_customer_selection")
    scope = cart["scope"]
    session = IgCommerceSelectionSession.objects.select_for_update().filter(pk=cart["session_id"],
        client=client, open_slot=1, state="open", generation=cart["generation"], revision=cart["selection_revision"],
        commercial_episode_id=scope["episode_id"]).first()
    if not session:
        _fail("selection_head_changed")
    watermark = cart.get("source_watermark") or {}
    if prior_snapshot is None and watermark.get("message_id") != latest_id:
        _fail("source_head_changed")
    extended = False
    if prior_snapshot is not None and cart != prior_snapshot["full_capture"]:
        extended = _purchase_extension(prior_snapshot["full_capture"], cart, latest_id, client, settings_obj, now)
        if not extended:
            _fail("source_configuration_or_head_changed")
    if (revision.active_slot != 1 or revision.state not in {"sealed", "claimed", "processed"}
        or revision.origin != "inbound") and not extended:
        _fail("revision_not_current")
    question_cart = prior_snapshot["full_capture"] if extended else cart
    owner = _business_owner(client, scope)
    # Only the ordinary quote owner's untouched legacy offer may be captured.
    # Source/current-basket proof above and the fresh owner fence below remain
    # exact; the later coordinator owns any actual flag/policy/revision upgrade.
    proposal_id = (owner.get("deal") or {}).get("active_checkout_proposal_id")
    if proposal_id:
        from management.models import IgCheckoutProposal, IgDeal
        from management.services.ig_checkout import assert_untouched_legacy_assisted_proposal, CheckoutConfigurationError
        deal = IgDeal.objects.select_for_update().filter(pk=owner["deal"]["pk"], client=client).first()
        proposal = IgCheckoutProposal.objects.select_for_update().filter(pk=proposal_id, client=client,
            deal=deal, commercial_episode_id=scope["episode_id"]).first() if deal else None
        if not proposal:
            _fail("existing_invoice_commitment_requires_review")
        try:
            assert_untouched_legacy_assisted_proposal(client=client, proposal=proposal, deal=deal,
                episode_id=scope["episode_id"])
        except CheckoutConfigurationError:
            _fail("existing_invoice_commitment_requires_review")
    catalog = resolve_source_cart_catalog(cart).as_dict()
    catalog_observations = (_catalog_configuration_observations(cart, catalog) if prior_snapshot is None
        else {line["line_id"]: deepcopy(line.get("catalog_item_spec")) for line in prior_snapshot["lines"]})
    inquiry = capture_product_inquiries(source_payloads, cart_capture=question_cart,
        readiness_by_line=catalog.get("readiness_by_line") or {}, profile_language=client.language)
    if inquiry.get("gap") or len(inquiry.get("inquiries") or []) > MAX_QUESTIONS:
        _fail(inquiry.get("gap") or "question_capture_bound")
    source_map = {proof["message_id"]: proof for proof in owned}
    questions = []
    line_ids = {row["line_id"] for row in cart["lines"]}
    for question in inquiry.get("inquiries") or []:
        proof = source_map.get(question.get("source_message_id"))
        if not proof or question.get("source_digest") != proof["digest"]:
            _fail("question_source_changed")
        if question.get("line_id") and question["line_id"] not in line_ids:
            _fail("question_line_changed")
        safe = {key: deepcopy(question.get(key)) for key in ("source_message_id", "source_digest", "line_id", "product_id",
            "axis", "requested_value", "question", "reason")}
        safe["question"] = _text(own_reply_text(safe["question"] or ""), limit=500, required=True)
        safe.update(question_id=_digest(safe), catalog_authority=False, checkout_authority=False,
            physical_authority=False, price_authority=False)
        questions.append(safe)
    lines = []
    for index, row in enumerate(cart["lines"]):
        preferences = source_line_preferences(cart, row, source_watermark=latest_id)
        if not preferences or row.get("index") != index:
            _fail("line_source_unavailable")
        # Values/proofs are canonical source observations, not manager terms.
        # No raw source body or private media URL is copied into the request.
        lines.append({"line_id": row["line_id"], "recipient_id": row["recipient_id"], "original_index": index,
            "source_choices": deepcopy(preferences["values"]), "source_evidence": deepcopy(preferences["evidence"]),
            "cleared": deepcopy(preferences["cleared"]), "quantity_default": deepcopy(row.get("defaults", {}).get("quantity")),
            "catalog_item_spec": deepcopy(catalog_observations.get(row["line_id"])),
            "draft_title": _text(own_reply_text(str(((catalog.get("readiness_by_line") or {}).get(row["line_id"], {}).get("product") or {}).get("title")
                or preferences["values"].get("model_query") or preferences["values"].get("garment_type") or ""))[:200], limit=200),
            "catalog_authority": False, "price_authority": False, "physical_authority": False,
            "purchase_authority": False})
    if prior_snapshot is not None:
        # Source questions and catalog hints are immutable observations. Stock
        # corrections cannot rewrite the customer's original review request.
        questions = deepcopy(prior_snapshot["questions"])
        for line, original in zip(lines, prior_snapshot["lines"], strict=True):
            if line["line_id"] != original["line_id"]:
                _fail("question_line_changed")
            line["draft_title"] = original["draft_title"]
    latest_payload = max(source_payloads, key=lambda row: row["message_id"])
    language = resolve_own_source_reply_language(sources=[latest_payload],
        history=[row for row in source_payloads if row is not latest_payload], profile_language=client.language,
        reset_floor=scope["reset_floor"], watermark_message_id=latest_id, watermark_event_at=now)
    if cart != capture_current_selection_lines(client.pk, now=now) or owner != _business_owner(client, scope):
        _fail("capture_read_conflict")
    _permission(client, settings_id, expected_settings_epoch=permission.settings_epoch,
        expected_client_epoch=permission.client_epoch)
    for member, proof in zip(frozen, owned, strict=True):
        second, reason = owned_source(revision, member.message, settings_obj, require_head=prior_snapshot is None)
        if not second or second[2] != proof:
            _fail(reason or "source_read_conflict")
    snapshot = {"schema": REQUEST_SCHEMA, "revision_id": revision.pk, "revision_digest": revision.snapshot_digest,
        "sources": owned, "source_id": latest_id, "source_head": deepcopy(watermark), "full_capture": cart,
        "lines": lines, "questions": questions, "owner_scope": owner, "settings_id": settings_id,
        "settings_permission_epoch": permission.settings_epoch, "client_permission_epoch": permission.client_epoch,
        "reply_language": language.as_dict(), "expires_at": (now + REQUEST_TTL).isoformat(),
        "authority": {"stock": False, "price": False, "purchase": False, "payment": False,
            "order": False, "provider_send": False}}
    return json.loads(_json(snapshot))


def _task(client, task_id):
    if task_id is None:
        return None
    from management.models import IgFollowUpTask
    task = IgFollowUpTask.objects.select_for_update().filter(pk=task_id, client=client, kind="manager_task").first()
    if not task or task.status in {"completed", "cancelled"}:
        _fail("manager_case_unavailable")
    return task


def _head(Request, client_id, intention_id, *, lock=False):
    rows = Request.objects.filter(client_id=client_id, intention_id=intention_id)
    return (rows.select_for_update() if lock else rows).order_by("-version").first()


def _publish_task_head(task, request, decision=None):
    if task is None:
        return
    head = {"request_id": str(request.pk), "request_version": request.version, "request_digest": request.envelope_hmac,
        "intention_id": str(request.intention_id), "decision_id": str(decision.pk) if decision else None,
        "decision_version": decision.version if decision else 0,
        "decision_digest": decision.envelope_hmac if decision else "",
        "authority": {"stock_written": False, "proposal_prepared": False, "customer_sent": False,
            "payment_confirmed": False, "order_created": False}}
    task.manager_context = {**(task.manager_context or {}), "assisted_commerce": head}
    task.save(update_fields=["manager_context", "updated_at"])


def create_assisted_request(revision_id, *, settings_id, operation_id, expected_revision_digest,
        expected_full_capture_digest, intention_id=None, expected_request_version=0,
        expected_request_digest="", task_id=None, now=None):
    """Producer from actual own revision/full current basket, no supplied rows.

    Revision/full-capture digests are optimistic expectations, not authority.
    The entire snapshot is freshly rebuilt below. Initial stable intention ID
    derives from source revision and canonical selection; later versions must
    explicitly reference the existing same-client intention and previous head.
    """
    from management.models import IgClient, IgCustomerTurnRevision
    from management.services.ig_commercial_episodes import commercial_episode_client_lock
    Request, _Decision, Execution, _Image = _models()
    operation = _uuid(operation_id)
    now = _now(now)
    if not _positive(revision_id) or not _positive(settings_id) or not _hash(expected_revision_digest) or not _hash(expected_full_capture_digest):
        _fail("request_expectation_invalid")
    if type(expected_request_version) is not int or expected_request_version < 0:
        _fail("request_version_invalid")
    identity = IgCustomerTurnRevision.objects.filter(pk=revision_id).values("client_id").first()
    if not identity:
        _fail("revision_missing")
    request_input = {"revision_id": revision_id, "settings_id": settings_id,
        "expected_revision_digest": expected_revision_digest, "expected_full_capture_digest": expected_full_capture_digest,
        "intention_id": str(intention_id) if intention_id else None, "expected_request_version": expected_request_version,
        "expected_request_digest": expected_request_digest, "task_id": task_id}
    input_digest = _digest(request_input)
    with commercial_episode_client_lock(identity["client_id"]), transaction.atomic():
        client = IgClient.objects.select_for_update().get(pk=identity["client_id"])
        task = _task(client, task_id)
        snapshot = _capture(client, revision_id, settings_id, now, expected_revision_digest=expected_revision_digest)
        if snapshot["full_capture"]["capture_digest"] != expected_full_capture_digest:
            _fail("full_capture_changed")
        prior_operation = Request.objects.filter(operation_id=operation).first()
        if prior_operation:
            if (not _verified(prior_operation, "request") or prior_operation.client_id != client.pk
                or prior_operation.envelope.get("operation_input_digest") != input_digest):
                _fail("operation_conflict")
            _current_request(prior_operation, client, now)
            return prior_operation, False
        intention = _uuid(intention_id) if intention_id else uuid.uuid5(uuid.NAMESPACE_URL,
            f"{PURPOSE}:{client.pk}:{revision_id}:{snapshot['full_capture']['capture_digest']}")
        if Request.objects.filter(intention_id=intention).exclude(client=client).exists():
            _fail("intention_owner_changed")
        head = _head(Request, client.pk, intention, lock=True)
        if ((head.version if head else 0) != expected_request_version
            or (head.envelope_hmac if head else "") != expected_request_digest
            or head and not _verified(head, "request")):
            _fail("request_head_changed")
        if intention_id and not head:
            _fail("intention_missing")
        if Execution.objects.filter(intention_id=intention).exists():
            _fail("executed_intention_requires_continuation_producer")
        if task and (task.manager_context or {}).get("assisted_commerce"):
            task_head = task.manager_context["assisted_commerce"]
            if not head or task_head.get("request_id") != str(head.pk) or task_head.get("request_digest") != head.envelope_hmac:
                _fail("manager_case_head_changed")
        owner = snapshot["owner_scope"]
        request = _seal(Request(intention_id=intention, version=expected_request_version + 1,
            operation_id=operation, client=client, task=task, source_id=snapshot["source_id"], revision_id=revision_id,
            episode_id=(owner.get("episode") or {}).get("pk"), deal_id=(owner.get("deal") or {}).get("pk"),
            source_namespace=snapshot["full_capture"]["scope"]["source_namespace"],
            reset_floor=snapshot["full_capture"]["scope"]["reset_floor"],
            client_permission_epoch=snapshot["client_permission_epoch"], full_capture_digest=expected_full_capture_digest),
            "request", snapshot, operation_input_digest=input_digest)
        _publish_task_head(task, request)
        return request, True


def _current_request(request, client, now):
    """The same strict source/head, privacy, epoch and owner checks at effects."""
    Request, _Decision, Execution, _Image = _models()
    if not _verified(request, "request") or request.client_id != client.pk:
        _fail("request_unverified")
    data = request.envelope["snapshot"]
    if request.created_at > now or now >= _date(data.get("expires_at")):
        _fail("request_expired")
    head = _head(Request, client.pk, request.intention_id)
    if not head or head.pk != request.pk:
        _fail("request_superseded")
    _permission(client, data["settings_id"], expected_settings_epoch=data["settings_permission_epoch"],
        expected_client_epoch=request.client_permission_epoch)
    actual = _capture(client, request.revision_id, data["settings_id"], now,
        expected_revision_digest=data["revision_digest"], prior_snapshot=data)
    current_capture = actual["full_capture"]
    # Original immutable sources remain the intention proof. A separately
    # authenticated later purchase source may extend canonical purchase intent
    # while all field choices, recipient identities and quantities stay exact.
    if current_capture != data["full_capture"]:
        actual["full_capture"] = data["full_capture"]
        actual["source_head"] = data["source_head"]
        for line, original in zip(actual["lines"], data["lines"], strict=True):
            for key in ("source_choices", "source_evidence", "cleared"):
                line[key] = original[key]
    # Expiry is sealed at initial capture, not renewed by this read.
    actual["expires_at"] = data["expires_at"]
    if (actual != data or actual["full_capture"]["capture_digest"] != request.full_capture_digest
        or request.source_id != data["source_id"] or request.source_namespace != actual["full_capture"]["scope"]["source_namespace"]
        or request.reset_floor != actual["full_capture"]["scope"]["reset_floor"]):
        _fail("request_source_or_owner_changed")
    task = _task(client, request.task_id)
    if task:
        head = (task.manager_context or {}).get("assisted_commerce") or {}
        if head.get("request_id") != str(request.pk) or head.get("request_digest") != request.envelope_hmac:
            _fail("manager_case_head_changed")
    if Execution.objects.filter(intention_id=request.intention_id).exists():
        # Includes provider-paid RESOURCE_REVIEW without a canonical Order.
        # Its active intention hold cannot be cleared by this decision service
        # or by a generic refund/manual-release action. The future continuation
        # producer needs the existing winner/resource reconciliation evidence.
        _fail("executed_intention_requires_execution_verifier")
    return data, task, current_capture


def _images(lines, request, now):
    _Request, _Decision, _Execution, Image = _models()
    identities = {}
    for line in lines:
        if line.image_id is not None:
            if line.image_id in identities and identities[line.image_id] != line.image_digest:
                _fail("image_identity_conflict")
            identities[line.image_id] = line.image_digest
    if not identities:
        return
    rows = list(Image.objects.filter(pk__in=identities, request=request, client_id=request.client_id).order_by("pk"))
    if {row.pk for row in rows} != set(identities):
        _fail("image_owner_changed")
    for image in rows:
        if (not _verified(image, "image") or image.state != "active" or image.erased_at is not None
            or image.delete_after <= now or image.content_digest != identities[image.pk]):
            _fail("image_unavailable")


def record_assisted_decision(request_id, *, actor, operation_id, status, expected_request_version,
        expected_request_digest, expected_full_capture_digest, expected_decision_version,
        expected_draft_digest, items=None, rejection_reason="", now=None):
    """Append DRAFT, approve identical current DRAFT, or reject under full CAS.

    Approval never performs commerce. A new draft supersedes the old approved
    head; rejection likewise invalidates future use but preserves old evidence.
    Already executed intentions are blocked until the dedicated continuation
    producer checks settled/in-flight coverage, avoiding reused approval units.
    """
    from management.models import IgClient, AdminAuditLog
    from management.services.ig_commercial_episodes import commercial_episode_client_lock
    Request, Decision, _Execution, _Image = _models()
    now, request_id, operation = _now(now), _uuid(request_id), _uuid(operation_id)
    if status not in {"draft", "approved", "rejected"}:
        _fail("decision_status_invalid")
    if type(expected_decision_version) is not int or expected_decision_version < 0:
        _fail("decision_version_invalid")
    authenticated_id = getattr(actor, "pk", None)
    input_digest = _digest({"request_id": str(request_id), "actor_id": authenticated_id, "status": status,
        "expected_request_version": expected_request_version, "expected_request_digest": expected_request_digest,
        "expected_full_capture_digest": expected_full_capture_digest, "expected_decision_version": expected_decision_version,
        "expected_draft_digest": expected_draft_digest, "items": items, "rejection_reason": rejection_reason})
    identity = Request.objects.filter(pk=request_id).values("client_id").first()
    if not identity:
        _fail("request_missing")
    with commercial_episode_client_lock(identity["client_id"]), transaction.atomic():
        actor = _actor(authenticated_id)
        client = IgClient.objects.select_for_update().get(pk=identity["client_id"])
        request = Request.objects.select_for_update().get(pk=request_id, client=client)
        data, task, _current_capture = _current_request(request, client, now)
        if (request.version != expected_request_version or request.envelope_hmac != expected_request_digest
            or request.full_capture_digest != expected_full_capture_digest):
            _fail("request_expectation_changed")
        prior_operation = Decision.objects.filter(operation_id=operation).first()
        if prior_operation:
            if (not _verified(prior_operation, "decision") or prior_operation.request_id != request.pk
                or prior_operation.actor_id != actor.pk or prior_operation.envelope.get("operation_input_digest") != input_digest):
                _fail("operation_conflict")
            if Decision.objects.filter(request=request).order_by("-version").values_list("pk", flat=True).first() != prior_operation.pk:
                _fail("decision_operation_superseded")
            return prior_operation, False
        latest = Decision.objects.select_for_update().filter(request=request).order_by("-version").first()
        if latest and not _verified(latest, "decision"):
            _fail("decision_unverified")
        if ((latest.version if latest else 0) != expected_decision_version
            or (latest.draft_digest if latest else "") != expected_draft_digest):
            _fail("decision_head_changed")
        previous_items = latest.envelope["snapshot"]["items"] if latest else None
        if status == "draft":
            if items is None:
                _fail("draft_items_required")
            lines = normalize_assisted_lines(items, source_lines=data["lines"], questions=data["questions"])
            reason = ""
        elif status == "approved":
            if not latest or latest.status != "draft":
                _fail("current_draft_required")
            lines = normalize_assisted_lines(previous_items, source_lines=data["lines"], questions=data["questions"],
                require_ready_answers=True)
            if items is not None and [line.as_dict() for line in normalize_assisted_lines(items,
                    source_lines=data["lines"], questions=data["questions"], require_ready_answers=True)] != previous_items:
                _fail("approved_draft_changed")
            reason = ""
        else:
            if not latest or items is not None:
                _fail("rejection_requires_current_decision")
            reason = _text(rejection_reason, limit=350, required=True, reason="rejection_reason_required")
            lines = normalize_assisted_lines(previous_items, source_lines=data["lines"], questions=data["questions"])
        normalized = [line.as_dict() for line in lines]
        draft_digest = _digest({"schema": DECISION_SCHEMA, "request_digest": request.envelope_hmac,
            "full_capture_digest": request.full_capture_digest, "items": normalized})
        if status == "approved" and draft_digest != expected_draft_digest:
            _fail("approved_draft_changed")
        _images(lines, request, now)
        total = sum((line.ready_total for line in lines), Decimal("0.00")) if status == "approved" else Decimal("0.00")
        quantity = sum(line.ready_capacity for line in lines if line.item_kind == "custom_apparel") if status == "approved" else 0
        physical_checked = now if status == "approved" and any(line.ready_capacity for line in lines) else None
        expires = min(_date(data["expires_at"]), now + APPROVAL_TTL) if status == "approved" else None
        snapshot = {"schema": DECISION_SCHEMA, "request_id": str(request.pk), "request_digest": request.envelope_hmac,
            "request_version": request.version, "full_capture_digest": request.full_capture_digest,
            "source_revision_digest": data["revision_digest"], "decision_version": expected_decision_version + 1,
            "status": status, "draft_digest": draft_digest, "items": normalized, "rejection_reason": reason,
            "physical_checked_at": physical_checked.isoformat() if physical_checked else None,
            "expires_at": expires.isoformat() if expires else None, "currency": "UAH", "approved_total": format(total, ".2f"),
            "approved_custom_quantity": quantity, "ready_line_ids": [line.line_id for line in lines if line.ready_capacity],
            "deferred_line_ids": [line.line_id for line in lines if not line.ready_capacity],
            # Approval describes a manager offer, not customer acceptance of
            # altered title/color/size/quantity or a requested alternative.
            "customer_terms_adoption_required": True,
            "quantity_authority": "exact_approved_quantity_once" if status == "approved" else "none",
            "authority": {"stock_write": False, "purchase": False, "proposal": False, "payment": False,
                "order": False, "provider_send": False}}
        decision = _seal(Decision(request=request, client=client, actor=actor, version=expected_decision_version + 1,
            operation_id=operation, status=status, request_digest=request.envelope_hmac, draft_digest=draft_digest,
            full_capture_digest=request.full_capture_digest, currency="UAH", approved_total=total,
            approved_custom_quantity=quantity, physical_checked_at=physical_checked, expires_at=expires),
            "decision", snapshot, operation_input_digest=input_digest)
        _publish_task_head(task, request, decision)
        AdminAuditLog.objects.create(actor=actor, action="ig_assisted_commerce_decision",
            entity_type="IgAssistedCommerceRequest", entity_id=str(request.pk),
            before={"decision_version": expected_decision_version}, after={"decision_id": str(decision.pk),
                "decision_version": decision.version, "status": decision.status, "decision_digest": decision.envelope_hmac,
                "snapshot_digest": decision.snapshot_digest}, reason="Exact source-bound assisted manager decision")
        return decision, True


@dataclass(frozen=True)
class VerifiedAssistedDecision:
    """Internally issued current decision proof; effects re-read its references."""
    request_id: uuid.UUID
    decision_id: uuid.UUID
    client_id: int
    actor_id: int
    approver_id: int
    intention_id: uuid.UUID
    request_version: int
    decision_version: int
    source_revision_id: int
    request_digest: str
    decision_digest: str
    full_capture_digest: str
    snapshot_digest: str
    approved_total: Decimal
    approved_custom_quantity: int
    physical_checked_at: datetime | None
    expires_at: datetime
    lines: tuple[AssistedDecisionLine, ...]
    _source_snapshot_json: str
    _current_capture_json: str
    proof_scope: str
    _issuer: object = field(repr=False, compare=False)

    def __post_init__(self):
        if self._issuer is not _ISSUER or self.proof_scope not in {"unconsumed", "execution", "historical"}:
            _fail("verified_decision_requires_native_producer")

    @property
    def historical(self):
        return self.proof_scope == "historical"

    @property
    def scope(self):
        return self.proof_scope

    @property
    def full_capture(self):
        return json.loads(self._source_snapshot_json)["full_capture"]

    @property
    def source_proof(self):
        return json.loads(self._source_snapshot_json)

    @property
    def current_capture(self):
        return json.loads(self._current_capture_json)

    @property
    def currency(self):
        return "UAH"

    @property
    def ready_lines(self):
        return tuple(line for line in self.lines if line.ready_capacity)


def load_verified_assisted_decision(request_id, decision_id, *, actor, expected_request_digest,
        expected_decision_digest, now=None):
    """Fresh native row reader. A stale typed object never substitutes this.

    Reentrant client barrier + nested transaction permit a future execution
    producer to call inside its existing owned transaction. Returned proof
    grants no capacity once the caller releases those locks. Existing execution
    and paid history need their dedicated frozen execution verifier, not this
    unconsumed approval path.
    """
    from management.models import IgClient
    from management.services.ig_commercial_episodes import commercial_episode_client_lock
    Request, Decision, _Execution, _Image = _models()
    request_id, decision_id, now = _uuid(request_id), _uuid(decision_id), _now(now)
    identity = Request.objects.filter(pk=request_id).values("client_id").first()
    if not identity:
        _fail("request_missing")
    with commercial_episode_client_lock(identity["client_id"]), transaction.atomic():
        executing_actor = _actor(getattr(actor, "pk", None))
        client = IgClient.objects.select_for_update().get(pk=identity["client_id"])
        request = Request.objects.select_for_update().get(pk=request_id, client=client)
        source_data, task, current_capture = _current_request(request, client, now)
        decision = Decision.objects.select_for_update().filter(pk=decision_id, request=request, client=client).first()
        latest = Decision.objects.filter(request=request).order_by("-version").values_list("pk", flat=True).first()
        if (not decision or decision.pk != latest or not _verified(decision, "decision")
            or request.envelope_hmac != expected_request_digest or decision.envelope_hmac != expected_decision_digest
            or decision.request_digest != request.envelope_hmac or decision.full_capture_digest != request.full_capture_digest
            or decision.status != "approved"):
            _fail("approved_decision_changed")
        _actor(decision.actor_id)  # Original approver's permissions may have been revoked.
        if (decision.created_at > now or not decision.expires_at or now >= decision.expires_at
            or decision.expires_at > _date(source_data["expires_at"])):
            _fail("physical_approval_expired")
        body = decision.envelope["snapshot"]
        lines = normalize_assisted_lines(body["items"], source_lines=source_data["lines"],
            questions=source_data["questions"], require_ready_answers=True)
        _images(lines, request, now)
        total = sum((line.ready_total for line in lines), Decimal("0.00"))
        quantity = sum(line.ready_capacity for line in lines if line.item_kind == "custom_apparel")
        if (total != decision.approved_total or quantity != decision.approved_custom_quantity
            or decision.currency != "UAH" or body["approved_total"] != format(total, ".2f")
            or body["approved_custom_quantity"] != quantity
            or body["request_digest"] != request.envelope_hmac
            or body["full_capture_digest"] != request.full_capture_digest
            or body["source_revision_digest"] != source_data["revision_digest"]
            or body["request_version"] != request.version or body["decision_version"] != decision.version
            or body["status"] != decision.status or body["draft_digest"] != decision.draft_digest
            or body["physical_checked_at"] != (decision.physical_checked_at.isoformat() if decision.physical_checked_at else None)
            or body["expires_at"] != decision.expires_at.isoformat()):
            _fail("approved_snapshot_changed")
        if any(line.ready_capacity for line in lines) and (not decision.physical_checked_at
            or decision.physical_checked_at > now or decision.physical_checked_at >= decision.expires_at
            or now - decision.physical_checked_at > APPROVAL_TTL):
            _fail("physical_approval_unverified")
        if task:
            head = task.manager_context["assisted_commerce"]
            if head.get("decision_id") != str(decision.pk) or head.get("decision_digest") != decision.envelope_hmac:
                _fail("manager_decision_head_changed")
        return VerifiedAssistedDecision(request.pk, decision.pk, client.pk, executing_actor.pk, decision.actor_id, request.intention_id,
            request.version, decision.version, request.revision_id, request.envelope_hmac,
            decision.envelope_hmac, request.full_capture_digest, decision.snapshot_digest, total, quantity,
            decision.physical_checked_at, decision.expires_at, lines, _json(source_data), _json(current_capture), "unconsumed", _ISSUER)


def revalidate_verified_assisted_decision(value, *, client=None, actor=None, now=None):
    """An effect owner reloads the capability while it holds its own locks."""
    from django.contrib.auth import get_user_model
    if not isinstance(value, VerifiedAssistedDecision) or value._issuer is not _ISSUER or value.historical:
        _fail("verified_decision_requires_native_producer")
    if client is not None and getattr(client, "pk", None) != value.client_id:
        _fail("verified_decision_client_changed")
    if value.proof_scope == "execution":
        Request, Decision, _Execution, _Image = _models()
        request = Request.objects.filter(pk=value.request_id, client_id=value.client_id).first()
        decision = Decision.objects.filter(pk=value.decision_id, request=request, client_id=value.client_id).first()
        if (not request or not decision or request.envelope_hmac != value.request_digest
            or decision.envelope_hmac != value.decision_digest):
            _fail("verified_decision_changed")
        # Read-only quote reconstruction. The proposal factory rejects this
        # scope; the native execution owner separately fences its own graph.
        return verified_assisted_decision_from_evidence(request, decision)
    executing = actor if actor is not None else get_user_model().objects.filter(pk=value.actor_id).first()
    fresh = load_verified_assisted_decision(value.request_id, value.decision_id, actor=executing,
        expected_request_digest=value.request_digest, expected_decision_digest=value.decision_digest, now=now)
    if fresh.snapshot_digest != value.snapshot_digest or fresh.intention_id != value.intention_id:
        _fail("verified_decision_changed")
    return fresh


def verify_assisted_evidence(row, kind):
    """Historical authenticity only; this function grants no new effect."""
    return kind in {"request", "decision", "execution", "image", "stock_sync"} and _verified(row, kind)


def verified_assisted_decision_from_evidence(request, decision, *, historical=False):
    """Verify frozen native terms after an execution owner's strong guard.

    This intentionally does not assert current/unconsumed/source ownership:
    the native execution producer/verifier owns that check and any controlled
    original-request -> own proposal/episode rebind. It accepts actual ORM rows
    and reloads them, never a dictionary, hash marker or provider reference.
    Historical terms preserve paid truth after expiry/revocation; they cannot
    enter the fresh quote revalidator or authorize another physical execution.
    """
    from management.models import IgClient
    Request, Decision, _Execution, Image = _models()
    if not isinstance(request, Request) or not isinstance(decision, Decision) or type(historical) is not bool:
        _fail("frozen_native_evidence_required")
    actual_request = Request.objects.filter(pk=request.pk).first()
    actual_decision = Decision.objects.filter(pk=decision.pk, request_id=request.pk).first()
    if (not actual_request or not actual_decision or not _verified(actual_request, "request")
        or not _verified(actual_decision, "decision") or actual_request.envelope_hmac != request.envelope_hmac
        or actual_decision.envelope_hmac != decision.envelope_hmac
        or actual_decision.client_id != actual_request.client_id
        or actual_decision.request_digest != actual_request.envelope_hmac
        or actual_decision.full_capture_digest != actual_request.full_capture_digest
        or actual_decision.status != "approved" or actual_decision.currency != "UAH" or not actual_decision.expires_at):
        _fail("frozen_assisted_evidence_invalid")
    request, decision = actual_request, actual_decision
    source, body = request.envelope["snapshot"], decision.envelope["snapshot"]
    lines = normalize_assisted_lines(body["items"], source_lines=source["lines"],
        questions=source["questions"], require_ready_answers=True)
    total = sum((line.ready_total for line in lines), Decimal("0.00"))
    quantity = sum(line.ready_capacity for line in lines if line.item_kind == "custom_apparel")
    if (body["request_digest"] != request.envelope_hmac or body["request_version"] != request.version
        or body["full_capture_digest"] != request.full_capture_digest
        or body["source_revision_digest"] != source["revision_digest"]
        or body["decision_version"] != decision.version or body["status"] != "approved"
        or body["draft_digest"] != decision.draft_digest
        or total != decision.approved_total or quantity != decision.approved_custom_quantity
        or body["approved_total"] != format(total, ".2f") or body["approved_custom_quantity"] != quantity
        or body["physical_checked_at"] != (decision.physical_checked_at.isoformat() if decision.physical_checked_at else None)
        or body["expires_at"] != decision.expires_at.isoformat()):
        _fail("frozen_approved_terms_invalid")
    now = _now()
    if not historical:
        client = IgClient.objects.filter(pk=request.client_id).first()
        _permission(client, source["settings_id"], expected_settings_epoch=source["settings_permission_epoch"],
            expected_client_epoch=request.client_permission_epoch)
        _actor(decision.actor_id)
        if decision.expires_at <= now or (any(line.ready_capacity for line in lines)
                and (not decision.physical_checked_at or not decision.physical_checked_at <= now < decision.expires_at)):
            _fail("physical_approval_expired")
        _images(lines, request, now)
    else:
        # Paid facts remain exact if an owned photo subsequently expires or is
        # erased. The media reader displays a placeholder; no URL/storage read.
        for line in lines:
            if line.image_id:
                image = Image.objects.filter(pk=line.image_id, request=request, client_id=request.client_id).first()
                if image and (not _verified(image, "image") or image.content_digest != line.image_digest):
                    _fail("frozen_image_identity_invalid")
    return VerifiedAssistedDecision(request.pk, decision.pk, request.client_id, decision.actor_id,
        decision.actor_id, request.intention_id, request.version, decision.version, request.revision_id,
        request.envelope_hmac, decision.envelope_hmac, request.full_capture_digest, decision.snapshot_digest,
        total, quantity, decision.physical_checked_at, decision.expires_at, lines,
        _json(source), _json(source["full_capture"]), "historical" if historical else "execution", _ISSUER)


def _editable_items(data):
    items = []
    for line in data["lines"]:
        choices = line["source_choices"]
        catalog = line.get("catalog_item_spec") or {}
        default = line.get("quantity_default") or {}
        quantity = choices.get("quantity")
        if quantity is None and default == {"value": 1, "authority": "existing_cart_default", "source_confirmed": False}:
            quantity = 1
        items.append({"line_id": line["line_id"], "recipient_id": line["recipient_id"],
            "original_index": line["original_index"], "item_kind": "catalog_apparel" if catalog else "custom_apparel",
            "product_id": catalog.get("product_id"), "color_variant_id": catalog.get("color_variant_id"),
            "title": catalog.get("title", line.get("draft_title", "")), "color": catalog.get("color", choices.get("color", "")),
            "size": catalog.get("size", choices.get("size", "")),
            "fit_code": catalog.get("fit_option_code", choices.get("fit_option_code", "")), "quantity": catalog.get("qty", quantity),
            "unit_price": catalog.get("unit_price"), "currency": "UAH", "availability": "clarify",
            "checked_quantity": None, "supply_basis": None, "source_location": "",
            "option_values": deepcopy(catalog.get("option_values") or {}), "preparation": None, "dispatch": None,
            "image_id": None, "image_digest": "", "question_answers": [{"question_id": question["question_id"],
                "state": "unknown", "answer": ""} for question in data["questions"] if question["line_id"] == line["line_id"]]})
    return items


def _decision_dto(decision):
    return {"id": str(decision.pk), "version": decision.version, "status": decision.status,
        "decision_digest": decision.envelope_hmac, "draft_digest": decision.draft_digest,
        "snapshot_digest": decision.snapshot_digest, "items": deepcopy(decision.envelope["snapshot"]["items"]),
        "physical_checked_at": decision.physical_checked_at.isoformat() if decision.physical_checked_at else None,
        "expires_at": decision.expires_at.isoformat() if decision.expires_at else None,
        "approved_total": format(decision.approved_total, ".2f"), "currency": decision.currency,
        "approved_custom_quantity": decision.approved_custom_quantity}


def operator_context(client_id, *, actor, request_id=None, now=None):
    """Current authenticated operator view, with exact CAS and no payable link."""
    from management.models import IgClient
    from management.services.ig_commercial_episodes import commercial_episode_client_lock
    from management.services.ig_reply_boundary import capture_reply_permission
    Request, Decision, _Execution, _Image = _models()
    _actor(getattr(actor, "pk", None))
    now = _now(now)
    base = {"status": "none", "reason": "", "request": None, "draft": None, "decision_head": None,
        "approved_head": None, "editable_items": [], "cas": {}, "can_approve": False,
        "can_prepare": False, "can_prepare_reason": "approved_decision_missing", "bot_effects_allowed": False}
    if not _positive(client_id):
        _fail("client_id_invalid")
    with commercial_episode_client_lock(client_id), transaction.atomic():
        client = IgClient.objects.select_for_update().filter(pk=client_id).first()
        if not client or client.privacy_erasure_started_at or client.hidden_at:
            return {**base, "status": "stale", "reason": "client_unavailable"}
        rows = Request.objects.filter(client=client)
        request = (rows.filter(pk=_uuid(request_id)).first() if request_id is not None
            else rows.order_by("-created_at", "-version", "pk").first())
        if request is None:
            return base
        if not _verified(request, "request"):
            return {**base, "status": "stale", "reason": "request_unverified"}
        data = request.envelope["snapshot"]
        base["request"] = {"id": str(request.pk), "version": request.version,
            "intention_id": str(request.intention_id), "request_digest": request.envelope_hmac,
            "full_capture_digest": request.full_capture_digest, "snapshot_digest": request.snapshot_digest,
            "source_revision_id": request.revision_id, "source_revision_digest": data["revision_digest"],
            "expires_at": data["expires_at"], "lines": deepcopy(data["lines"]), "questions": deepcopy(data["questions"])}
        latest = Decision.objects.filter(request=request).order_by("-version").first()
        if latest and not _verified(latest, "decision"):
            return {**base, "status": "stale", "reason": "decision_unverified"}
        draft = Decision.objects.filter(request=request, status="draft").order_by("-version").first()
        if draft and _verified(draft, "decision"):
            base["draft"] = _decision_dto(draft)
        if latest:
            base["decision_head"] = _decision_dto(latest)
        base["editable_items"] = deepcopy(latest.envelope["snapshot"]["items"]) if latest else _editable_items(data)
        base["item_controls"] = [{"line_id": item["line_id"], "can_edit_price": item["item_kind"] == "custom_apparel",
            "can_edit_terms": item["item_kind"] == "custom_apparel", "can_edit_photo": item["item_kind"] == "custom_apparel",
            "product_id": item["product_id"], "color_variant_id": item["color_variant_id"],
            "price_is_catalog_hint": item["item_kind"] == "catalog_apparel"} for item in base["editable_items"]]
        base["cas"] = {"expected_request_version": request.version, "expected_request_digest": request.envelope_hmac,
            "expected_full_capture_digest": request.full_capture_digest, "expected_decision_version": latest.version if latest else 0,
            "expected_draft_digest": latest.draft_digest if latest else ""}
        try:
            _current_request(request, client, now)
        except AssistedDecisionError as exc:
            return {**base, "status": "stale", "reason": exc.reason, "can_prepare_reason": exc.reason}
        base["status"] = "current"
        base["can_approve"] = bool(latest and latest.status == "draft")
        base["bot_effects_allowed"] = bool(capture_reply_permission(data["settings_id"], client.pk))
        if latest and latest.status == "approved":
            try:
                verified = load_verified_assisted_decision(request.pk, latest.pk, actor=actor,
                    expected_request_digest=request.envelope_hmac, expected_decision_digest=latest.envelope_hmac, now=now)
            except AssistedDecisionError as exc:
                return {**base, "status": "stale", "reason": exc.reason, "can_prepare_reason": exc.reason}
            base["approved_head"] = _decision_dto(latest)
            base["can_prepare"] = bool(verified.lines and all(line.availability == "ready" for line in verified.lines))
            base["can_prepare_reason"] = "" if base["can_prepare"] else "full_ready_required"
        return base
