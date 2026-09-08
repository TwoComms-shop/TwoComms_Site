"""Durable, bounded authority bindings for revision delivery effects.

The bindings contain only identifiers, validated catalog selectors, and
content-free digests.  They are designed for ``ig_revision_outbox``'s
``fact_checker``/``offer_checker`` callbacks and deliberately do not accept a
model price, chat agreement, customer text, or bearer URL as authority.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable, Mapping
from uuid import UUID


SCHEMA_VERSION = 1
MAX_PRODUCTS = 8
MAX_CLAIMS = 8
MAX_PROPOSAL_ITEMS = 32
MAX_SHIPMENTS = 32
MAX_VARIANTS = 32
_HASH_RE = re.compile(r"[0-9a-f]{64}")
_SELECTOR_CODE_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")

CLAIM_CATALOG_CONFIGURATION = "catalog_configuration"
CLAIM_CATALOG_ASSET_REFERENCES = "catalog_asset_references"
CLAIM_CANONICAL_URLS = "canonical_urls"
CLAIM_PUBLIC_POLICY_INPUTS = "public_policy_inputs"
CLAIM_CURRENT_OFFER = "current_offer"
CLAIM_PAYMENT = "payment"
CLAIM_ORDER = "order"
CLAIM_SHIPMENT = "shipment"

FACT_CLAIMS = frozenset({
    CLAIM_CATALOG_ASSET_REFERENCES,
    CLAIM_CATALOG_CONFIGURATION,
    CLAIM_CANONICAL_URLS,
    CLAIM_PUBLIC_POLICY_INPUTS,
    CLAIM_PAYMENT,
    CLAIM_ORDER,
    CLAIM_SHIPMENT,
})
OFFER_CLAIMS = frozenset({CLAIM_CURRENT_OFFER})
SUPPORTED_CLAIMS = FACT_CLAIMS | OFFER_CLAIMS

# These are semantic capabilities for a future server-side caller.  They are
# never populated from provider controls.  Existing domain gates remain the
# authority for whether an operation may be attempted.
ACTION_REQUIRED_CLAIMS = {
    "client_configuration_update": frozenset({CLAIM_CATALOG_CONFIGURATION}),
    "checkout_proposal_create": frozenset({CLAIM_CATALOG_CONFIGURATION}),
    "follow_decision_prepare": frozenset({CLAIM_PUBLIC_POLICY_INPUTS}),
    "manager_escalation_intent": frozenset({CLAIM_PUBLIC_POLICY_INPUTS}),
    "order_fulfillment": frozenset({
        CLAIM_CURRENT_OFFER, CLAIM_PAYMENT, CLAIM_ORDER
    }),
    "prize_review_case_create": frozenset({CLAIM_PUBLIC_POLICY_INPUTS}),
    "size_gap_notification_intent": frozenset({CLAIM_CATALOG_CONFIGURATION}),
    "spam_transition": frozenset({CLAIM_PUBLIC_POLICY_INPUTS}),
}


@dataclass(frozen=True)
class RevisionAuthorityBindingSet:
    ready: bool
    reasons: tuple[str, ...] = ()
    allowed_actions: tuple[str, ...] = ()
    fact_bindings: tuple[dict, ...] = ()
    offer_bindings: tuple[dict, ...] = ()
    authority_digest: str = ""


def _jsonable(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {
            str(key): _jsonable(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _canonical(value) -> bytes:
    return json.dumps(
        _jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _digest(value) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _text_digest(value) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _positive_id(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _product_ids(raw) -> tuple[int, ...] | None:
    if isinstance(raw, (list, tuple)):
        parts = list(raw)
    elif isinstance(raw, str):
        parts = raw.replace(";", ",").split(",")
    else:
        return None
    values: list[int] = []
    for raw_value in parts:
        value = _positive_id(raw_value)
        if value is None or value in values:
            return None
        values.append(value)
    return tuple(values) if values else None


def _selector_code(value) -> str | None:
    normalized = str(value or "").strip().casefold()
    return normalized if _SELECTOR_CODE_RE.fullmatch(normalized) else None


def _configuration_selector(client, control) -> tuple[dict | None, str]:
    candidate = dict(control) if isinstance(control, Mapping) else {}
    supplied = []
    for key in ("product", "product_id"):
        if key in candidate:
            supplied.append(_positive_id(candidate.get(key)))
    if supplied and (None in supplied or len(set(supplied)) != 1):
        return None, "catalog_selector_invalid"
    product_id = supplied[0] if supplied else _positive_id(
        getattr(client, "current_product_id", None)
    )
    if product_id is None:
        return None, "catalog_selector_missing"

    selector: dict[str, object] = {"product": product_id}
    variant_values = []
    for key in ("variant", "color_variant_id"):
        if key in candidate:
            variant_values.append(_positive_id(candidate.get(key)))
    if variant_values:
        if None in variant_values or len(set(variant_values)) != 1:
            return None, "catalog_selector_invalid"
        selector["variant"] = variant_values[0]

    if "fit" in candidate:
        fit = _selector_code(candidate.get("fit"))
        if fit is None:
            return None, "catalog_selector_invalid"
        selector["fit"] = fit

    from management.services.instagram_bot import _control_option_values

    options = _control_option_values(candidate)
    if options is None or len(options) > 12:
        return None, "catalog_selector_invalid"
    normalized_options = {}
    for key, value in sorted(options.items()):
        normalized_key = _selector_code(key)
        normalized_value = _selector_code(value)
        if normalized_key is None or normalized_value is None:
            return None, "catalog_selector_invalid"
        normalized_options[normalized_key] = normalized_value
    if normalized_options:
        selector["options"] = [
            f"{key}={value}" for key, value in normalized_options.items()
        ]
    if "size" in candidate:
        from product_catalog.size_grid_services import normalize_size_value

        size = normalize_size_value(candidate.get("size"))
        if not size or len(size) > 16:
            return None, "catalog_size_invalid"
        selector["size"] = size
    if "qty" in candidate:
        qty = _positive_id(candidate.get("qty"))
        if qty is None or qty > 50:
            return None, "catalog_quantity_invalid"
        selector["qty"] = qty
    return selector, ""


def _url_selector(control) -> tuple[dict | None, str]:
    candidate = dict(control) if isinstance(control, Mapping) else {}
    if candidate.get("catalog_link") is not True:
        return None, "canonical_url_not_requested"
    raw = candidate.get("show_products")
    if raw in (None, ""):
        raw = candidate.get("product") or candidate.get("product_id")
        product_id = _positive_id(raw)
        ids = (product_id,) if product_id else None
    else:
        ids = _product_ids(raw)
    if not ids or len(ids) > MAX_PRODUCTS:
        return None, "catalog_selector_invalid"
    return {"catalog_link": True, "show_products": ",".join(map(str, ids))}, ""


def _fresh_client(client):
    from management.models import IgClient

    client_id = _positive_id(getattr(client, "pk", None))
    if client_id is None:
        return None
    return IgClient.objects.filter(pk=client_id).first()


def _fresh_settings(settings_obj):
    from management.models import InstagramBotSettings

    settings_id = _positive_id(getattr(settings_obj, "pk", None))
    if settings_id is None:
        return None
    return (
        InstagramBotSettings.objects.select_related(
            "active_instruction_publication"
        )
        .filter(pk=settings_id)
        .first()
    )


def _episode_and_offer(client):
    from management.services.ig_reply_authority import (
        _current_episode,
        _current_proposal,
        _episode_order,
    )

    episode = _current_episode(client)
    proposal = _current_proposal(episode) if episode is not None else None
    order, order_gap = _episode_order(episode) if episode is not None else (None, "")
    return episode, proposal, order, order_gap


def _base_binding(claim: str, client, episode, selector, projection, **subjects):
    return {
        "kind": "ig_revision_authority",
        "schema_version": SCHEMA_VERSION,
        "claim": claim,
        "client_id": int(client.pk),
        "episode_id": int(episode.pk) if episode is not None else 0,
        "selector": selector,
        "subjects": subjects,
        "authority_digest": _digest(projection),
    }


def _episode_projection(episode) -> dict:
    if episode is None:
        return {}
    return {
        "id": episode.pk,
        "client_id": episode.client_id,
        "sequence": episode.sequence,
        "open_slot": episode.open_slot,
        "state": episode.state,
        "deal_id": episode.deal_id,
        "payment_review_id": episode.primary_payment_review_id,
        "order_attribution_id": episode.order_attribution_id,
        "intended_order_id": episode.intended_order_id,
    }


def _persisted_configuration_projection(client) -> tuple[dict | None, str]:
    """Return bounded current client selection without retaining customer text."""
    from management.services.instagram_bot import _checkout_selection_state
    from product_catalog.size_grid_services import normalize_size_value

    product_id = _positive_id(getattr(client, "current_product_id", None)) or 0
    selection = _checkout_selection_state(client, product_id) if product_id else {}
    variant_id = _positive_id(selection.get("color_variant_id")) or 0
    fit = str(selection.get("fit_option_code") or "").strip().casefold()
    if fit and _selector_code(fit) is None:
        return None, "persisted_configuration_invalid"
    options = selection.get("option_values") or {}
    if not isinstance(options, Mapping) or len(options) > 12:
        return None, "persisted_configuration_invalid"
    normalized_options = {}
    for key, value in sorted(options.items()):
        normalized_key = _selector_code(key)
        normalized_value = _selector_code(value)
        if normalized_key is None or normalized_value is None:
            return None, "persisted_configuration_invalid"
        normalized_options[normalized_key] = normalized_value
    raw_size = str(getattr(client, "current_size", "") or "").strip()
    size = normalize_size_value(raw_size) if raw_size else ""
    if raw_size and (not size or len(size) > 16):
        return None, "persisted_configuration_invalid"
    try:
        qty = int(getattr(client, "current_qty", 1) or 1)
    except (TypeError, ValueError):
        return None, "persisted_configuration_invalid"
    if qty < 1 or qty > 50:
        return None, "persisted_configuration_invalid"
    return {
        "product_id": product_id,
        "variant_id": variant_id,
        "fit_code": fit,
        "size": size,
        "quantity": qty,
        "option_values_digest": _digest(normalized_options),
    }, ""


def _checkout_configuration_binding(client, control, episode):
    """An exact bounded cart, independently repriced by the checkout resolver."""
    from management.services.ig_checkout import CheckoutConfigurationError, validate_checkout_items

    items = control.get("items")
    keys = {"product_id", "color_variant_id", "qty", "size", "fit_option_code", "option_values"}
    if not isinstance(items, (list, tuple)) or not 1 <= len(items) <= MAX_PRODUCTS:
        return None, "checkout_items_invalid"
    if any(not isinstance(row, Mapping) or set(row) - keys for row in items):
        return None, "checkout_items_invalid"
    try:
        quote = validate_checkout_items(client=client, item_specs=list(items), negotiated_total=None)
    except CheckoutConfigurationError as exc:
        return None, "checkout:" + exc.code
    selector = {"items": [{
        "product_id": item.product.pk,
        "color_variant_id": item.color_variant.pk if item.color_variant else None,
        "qty": item.quantity, "size": item.size,
        "fit_option_code": item.fit_code, "option_values": dict(item.option_values),
    } for item in quote.items]}
    persisted, reason = _persisted_configuration_projection(client)
    if reason:
        return None, reason
    return _base_binding(
        CLAIM_CATALOG_CONFIGURATION, client, episode, selector,
        {"items": [item.digest_payload() for item in quote.items],
         "persisted_selection": persisted, "quote_digest": quote.digest},
        product_ids=sorted({item.product.pk for item in quote.items}),
        variant_ids=sorted({item.color_variant.pk for item in quote.items if item.color_variant}),
        item_count=len(quote.items),
    ), ""


def _catalog_configuration_binding(client, control, episode):
    if "items" in control:
        return _checkout_configuration_binding(client, control, episode)
    selector, reason = _configuration_selector(client, control)
    if reason:
        return None, reason
    from management.services.ig_reply_authority import (
        _current_configuration_pricing,
        build_reply_truth_context,
    )

    authority_control = dict(selector)
    pricing, invalid = _current_configuration_pricing(client, authority_control)
    context = build_reply_truth_context(client, control=authority_control)
    if (
        invalid
        or pricing is None
        or "current_configuration_price" not in context.evidence_codes
    ):
        return None, "catalog_configuration_unavailable"

    if "size" in selector:
        size = str(selector["size"])
        configurations = list(pricing.get("configurations", ()))
        compatible = [
            row
            for row in configurations
            if row.get("has_compatible_size_contract")
            and size in tuple(row.get("compatible_sizes") or ())
        ]
        if not compatible:
            reason = (
                "catalog_size_unavailable"
                if any(
                    row.get("has_compatible_size_contract")
                    for row in configurations
                )
                else "catalog_size_unverified"
            )
            return None, reason

    persisted, reason = _persisted_configuration_projection(client)
    if reason:
        return None, reason

    product_id = int(selector["product"])
    variants = sorted({
        int(row["variant_id"])
        for row in pricing.get("configurations", ())
        if row.get("variant_id")
    })
    if len(variants) > MAX_VARIANTS:
        return None, "variant_limit"
    from management.models import IgCommerceSelectionSession

    session = IgCommerceSelectionSession.objects.filter(client=client, open_slot=1).order_by("-generation").first()
    session_binding = ({"id": session.pk, "revision": session.revision, "snapshot_digest": _digest(session.snapshot())} if session is not None else {})
    projection = {
        "product_id": product_id,
        "selector": selector,
        "persisted_selection": persisted,
        "selection_session": session_binding,
        "exact": bool(pricing.get("exact")),
        "minimum": pricing.get("minimum"),
        "maximum": pricing.get("maximum"),
        "configurations": [
            {
                "variant_id": row.get("variant_id"),
                "color_id": row.get("color_id"),
                "option_values": row.get("option_values") or {},
                "fit_code": row.get("fit_code") or "",
                "compatible_sizes": row.get("compatible_sizes") or (),
                "price": row.get("price"),
                "stock": row.get("stock"),
            }
            for row in pricing.get("configurations", ())
        ],
        "currency": tuple(context.allowed_currency_codes),
    }
    return _base_binding(
        CLAIM_CATALOG_CONFIGURATION,
        client,
        episode,
        selector,
        projection,
        product_ids=[product_id],
        variant_ids=variants,
        variant_count=len(variants),
    ), ""


def _canonical_urls_binding(client, control, episode):
    selector, reason = _url_selector(control)
    if reason:
        return None, reason
    from management.services.ig_reply_authority import build_reply_truth_context

    ids = tuple(int(value) for value in selector["show_products"].split(","))
    context = build_reply_truth_context(client, control=selector)
    url_digests = sorted(_digest(url) for url in context.authorized_urls)
    if len(url_digests) != len(ids):
        return None, "canonical_url_unavailable"
    projection = {"product_ids": ids, "url_digests": url_digests}
    return _base_binding(
        CLAIM_CANONICAL_URLS,
        client,
        episode,
        selector,
        projection,
        product_ids=list(ids),
    ), ""


def _public_policy_binding(client, settings_obj):
    settings_row = _fresh_settings(settings_obj)
    if settings_row is None:
        return None, "public_policy_unavailable"
    language = str(getattr(client, "language", "") or "uk").strip().casefold()
    if language not in {"uk", "ru", "en"}:
        language = "uk"
    try:
        from management.services.approved_public_facts import (
            APPROVED_PUBLIC_FACTS_VERSION,
        )
        from management.services.bot_knowledge import read_knowledge_manifest
        from management.services.ig_policy_publication import (
            load_active_policy_snapshot,
        )

        knowledge = read_knowledge_manifest(language)
        publication = load_active_policy_snapshot(settings_row)
    except Exception:
        return None, "public_policy_unavailable"
    projection = {
        "language": language,
        "response_behavior_hash": _digest({
            "ai_enabled": bool(settings_row.ai_enabled),
            "trigger_text": str(settings_row.trigger_text or ""),
            "reply_text": str(settings_row.reply_text or ""),
        }),
        "core_prompt_hash": _text_digest(
            str(settings_row.system_prompt or "").strip()
        ),
        "directives_hash": _text_digest(
            str(settings_row.knowledge_base or "").strip()
        ),
        "knowledge_version": APPROVED_PUBLIC_FACTS_VERSION,
        "knowledge_hash": str(knowledge.content_hash),
        "publication_id": publication.publication_id,
        "publication_version": publication.version,
        "publication_hash": publication.snapshot_hash,
        "publication_compiler_version": publication.compiler_version,
    }
    return _base_binding(
        CLAIM_PUBLIC_POLICY_INPUTS,
        client,
        None,
        {"language": language},
        projection,
        settings_id=int(settings_row.pk),
        core_prompt_hash=projection["core_prompt_hash"],
        directives_hash=projection["directives_hash"],
        response_behavior_hash=projection["response_behavior_hash"],
        knowledge_hash=projection["knowledge_hash"],
        knowledge_version=projection["knowledge_version"],
        publication_id=int(publication.publication_id),
        publication_version=int(publication.version),
        publication_hash=publication.snapshot_hash,
        publication_compiler_version=publication.compiler_version,
    ), ""


def _proposal_projection(proposal):
    items = list(
        proposal.items.order_by("position", "id")[: MAX_PROPOSAL_ITEMS + 1]
    )
    if len(items) > MAX_PROPOSAL_ITEMS:
        return None
    product_ids = {item.product_id for item in items if item.product_id}
    if len(product_ids) > MAX_PRODUCTS:
        return None
    return {
        "proposal_id": proposal.pk,
        "client_id": proposal.client_id,
        "deal_id": proposal.deal_id,
        "episode_id": proposal.commercial_episode_id,
        # Opening the same offer changes only viewing telemetry.
        "status": "ready" if proposal.status in {"ready", "viewed"} else proposal.status,
        "revision": proposal.revision,
        "currency": proposal.currency,
        "catalog_total": proposal.catalog_total,
        "negotiated_discount": proposal.negotiated_discount,
        "quoted_total": proposal.quoted_total,
        "requested_payment_amount": proposal.requested_payment_amount,
        "pay_type": proposal.pay_type,
        "allow_promo": proposal.allow_promo,
        "items_digest": proposal.items_digest,
        "expires_at": proposal.expires_at,
        "assisted_checkout_v2": proposal.assisted_checkout_v2,
        "payment_policy": proposal.payment_policy,
        "payment_policy_evidence_message_id": proposal.payment_policy_evidence_message_id,
        "payment_policy_evidence_kind": proposal.payment_policy_evidence_kind,
        "payment_policy_evidence_revision": proposal.payment_policy_evidence_revision,
        "payment_policy_evidence_digest": proposal.payment_policy_evidence_digest,
        "custom_print_full_only": proposal.custom_print_full_only,
        "items": [
            {
                "id": item.pk,
                "product_id": item.product_id,
                "variant_id": item.color_variant_id,
                "sku": item.sku,
                "color_code": item.color_code,
                "size": item.size,
                "fit_code": item.fit_code,
                "option_values": item.option_values,
                "quantity": item.quantity,
                "catalog_unit_price": item.catalog_unit_price,
                "catalog_line_total": item.catalog_line_total,
                "quoted_unit_price": item.quoted_unit_price,
                "quoted_line_total": item.quoted_line_total,
                "price_source": item.price_source,
                "position": item.position,
            }
            for item in items
        ],
    }


def _current_offer_binding(client, episode, proposal, control=None):
    if episode is None or proposal is None:
        return None, "current_offer_unavailable"
    from django.utils import timezone
    from management.models import IgCheckoutAccessToken

    if proposal.expires_at <= timezone.now():
        return None, "current_offer_expired"
    selector = {}
    token_projection = None
    if "checkout_access_token_id" in (control or {}):
        token_id = _positive_id(control.get("checkout_access_token_id"))
        token = IgCheckoutAccessToken.objects.filter(
            pk=token_id, proposal=proposal, revoked_at__isnull=True,
            expires_at__gt=timezone.now(), kind=IgCheckoutAccessToken.Kind.BOT,
        ).first()
        if token is None:
            return None, "checkout_access_token_unavailable"
        selector = {"checkout_access_token_id": token.pk}
        token_projection = {"id": token.pk, "proposal_id": token.proposal_id,
                            "expires_at": token.expires_at, "kind": token.kind,
                            "token_digest": token.token_digest}
    proposal_projection = _proposal_projection(proposal)
    if proposal_projection is None:
        return None, "authority_row_limit"
    projection = {
        "episode": _episode_projection(episode),
        "proposal": proposal_projection,
        "checkout_access_token": token_projection,
    }
    product_ids = sorted({
        int(item["product_id"])
        for item in proposal_projection["items"]
        if item.get("product_id")
    })
    variant_ids = sorted({
        int(item["variant_id"])
        for item in proposal_projection["items"]
        if item.get("variant_id")
    })
    return _base_binding(
        CLAIM_CURRENT_OFFER,
        client,
        episode,
        selector,
        projection,
        deal_id=int(proposal.deal_id),
        proposal_id=int(proposal.pk),
        proposal_revision=int(proposal.revision),
        product_ids=product_ids,
        variant_ids=variant_ids,
    ), ""


def _business_fact_binding(claim, client, episode, order, order_gap):
    if episode is None:
        return None, "current_episode_unavailable"
    if order_gap:
        return None, order_gap
    from management.models import IgOrderShipment
    from management.services.ig_reply_authority import build_reply_truth_context

    context = build_reply_truth_context(client)
    subjects = {"order_id": int(order.pk) if order is not None else 0}
    if claim == CLAIM_PAYMENT:
        projection = {
            "episode": _episode_projection(episode),
            "payment_confirmed": context.payment_confirmed,
            "evidence": sorted(
                code for code in context.evidence_codes if "payment" in code
            ),
            "deal_id": int(episode.deal_id or 0),
            "payment_review_id": int(episode.primary_payment_review_id or 0),
            "order_id": subjects["order_id"],
            "order_payment_status": str(getattr(order, "payment_status", "") or ""),
        }
    elif claim == CLAIM_ORDER:
        projection = {
            "episode": _episode_projection(episode),
            "order_created": context.order_created,
            "order_id": subjects["order_id"],
            "deal_id": int(episode.deal_id or 0),
            "order_status": str(getattr(order, "status", "") or ""),
            "order_payment_status": str(getattr(order, "payment_status", "") or ""),
            "order_total": getattr(order, "total_sum", None),
            "order_pay_type": str(getattr(order, "pay_type", "") or ""),
        }
    else:
        shipment_rows = []
        if order is not None:
            shipment_rows = list(
                IgOrderShipment.objects.filter(order_id=order.pk)
                .order_by("created_at", "id")
                .values(
                    "id", "direction", "purpose", "supersedes_id", "source",
                    "payer", "reuses_outbound_tracking", "tracking_number",
                )
                [: MAX_SHIPMENTS + 1]
            )
            if len(shipment_rows) > MAX_SHIPMENTS:
                return None, "authority_row_limit"
        projection = {
            "episode": _episode_projection(episode),
            "shipment_state": context.shipment_state,
            "tracking_digests": sorted(
                _digest(value) for value in context.known_tracking_refs
            ),
            "order_id": subjects["order_id"],
            "order_status": str(getattr(order, "status", "") or ""),
            "tracking_status_code": getattr(order, "tracking_status_code", None),
            "shipment_rows": shipment_rows,
        }
    return _base_binding(
        claim, client, episode, {}, projection, **subjects
    ), ""


def build_revision_authority_bindings(
    client,
    *,
    claims: Iterable[str],
    control: Mapping[str, object] | None = None,
    server_authorized_actions: Iterable[str] = (),
    settings_obj=None,
) -> RevisionAuthorityBindingSet:
    """Build current DB authority for explicitly requested protected claims.

    ``server_authorized_actions`` is reserved for a caller that has already
    passed the relevant domain/business gate.  Provider output must never be
    supplied as this argument.
    """
    fresh = _fresh_client(client)
    if fresh is None:
        return RevisionAuthorityBindingSet(False, ("client_unavailable",))
    requested = tuple(dict.fromkeys(str(value or "") for value in claims or ()))
    if not requested or len(requested) > MAX_CLAIMS:
        return RevisionAuthorityBindingSet(False, ("claim_count_invalid",))
    if any(claim not in SUPPORTED_CLAIMS for claim in requested):
        return RevisionAuthorityBindingSet(False, ("claim_invalid",))

    requested_set = frozenset(requested)
    actions = tuple(sorted(set(str(value or "") for value in server_authorized_actions)))
    if any(action not in ACTION_REQUIRED_CLAIMS for action in actions):
        return RevisionAuthorityBindingSet(False, ("action_invalid",))
    if any(not ACTION_REQUIRED_CLAIMS[action].issubset(requested_set) for action in actions):
        return RevisionAuthorityBindingSet(False, ("action_claim_missing",))

    # Model-controlled prices are never persisted into or consulted by this
    # adapter.  Catalog/configuration authority is independently re-derived.
    safe_control = dict(control) if isinstance(control, Mapping) else {}
    safe_control.pop("price", None)
    safe_control.pop("price_quoted", None)

    episode, proposal, order, order_gap = _episode_and_offer(fresh)
    facts: list[dict] = []
    offers: list[dict] = []
    reasons: list[str] = []
    for claim in requested:
        if claim == CLAIM_CATALOG_CONFIGURATION:
            binding, reason = _catalog_configuration_binding(
                fresh, safe_control, episode
            )
        elif claim == CLAIM_CATALOG_ASSET_REFERENCES:
            from management.services.ig_revision_catalog_assets import build_catalog_asset_reference_binding

            binding, reason = build_catalog_asset_reference_binding(fresh, safe_control)
        elif claim == CLAIM_CANONICAL_URLS:
            binding, reason = _canonical_urls_binding(fresh, safe_control, episode)
        elif claim == CLAIM_PUBLIC_POLICY_INPUTS:
            binding, reason = _public_policy_binding(fresh, settings_obj)
        elif claim == CLAIM_CURRENT_OFFER:
            binding, reason = _current_offer_binding(fresh, episode, proposal, safe_control)
        else:
            binding, reason = _business_fact_binding(
                claim, fresh, episode, order, order_gap
            )
        if reason:
            if reason not in reasons:
                reasons.append(reason)
            continue
        (offers if claim in OFFER_CLAIMS else facts).append(binding)

    if reasons:
        return RevisionAuthorityBindingSet(False, tuple(reasons))
    if "order_fulfillment" in actions:
        from management.services.ig_reply_authority import build_reply_truth_context

        current = build_reply_truth_context(fresh)
        if not current.payment_confirmed or not current.order_created:
            return RevisionAuthorityBindingSet(
                False, ("action_authority_unavailable",)
            )
    combined = sorted((*facts, *offers), key=lambda item: item["claim"])
    return RevisionAuthorityBindingSet(
        True,
        allowed_actions=actions,
        fact_bindings=tuple(facts),
        offer_bindings=tuple(offers),
        authority_digest=_digest(combined),
    )


def _check_bindings(
    bindings, *, expected_claims, revision, client, settings_obj=None
) -> bool:
    if (
        _positive_id(getattr(client, "pk", None)) is None
        or _positive_id(getattr(revision, "client_id", None)) != int(client.pk)
    ):
        return False
    rows = list(bindings or ())
    if not rows or len(rows) > MAX_CLAIMS:
        return False
    seen = set()
    for expected in rows:
        if not isinstance(expected, Mapping):
            return False
        claim = str(expected.get("claim") or "")
        if (
            expected.get("kind") != "ig_revision_authority"
            or expected.get("schema_version") != SCHEMA_VERSION
            or claim not in expected_claims
            or claim in seen
            or _positive_id(expected.get("client_id")) != int(client.pk)
            or not _HASH_RE.fullmatch(str(expected.get("authority_digest") or ""))
        ):
            return False
        selector = expected.get("selector")
        if not isinstance(selector, Mapping):
            return False
        rebuilt = build_revision_authority_bindings(
            client,
            claims=(claim,),
            control=dict(selector),
            settings_obj=settings_obj,
        )
        current_rows = (
            rebuilt.offer_bindings if claim in OFFER_CLAIMS else rebuilt.fact_bindings
        )
        if not rebuilt.ready or len(current_rows) != 1 or current_rows[0] != dict(expected):
            return False
        seen.add(claim)
    return True


def check_fact_bindings(bindings, *, revision, client, settings_obj=None) -> bool:
    return _check_bindings(
        bindings,
        expected_claims=FACT_CLAIMS,
        revision=revision,
        client=client,
        settings_obj=settings_obj,
    )


def check_offer_bindings(bindings, *, revision, client, settings_obj=None) -> bool:
    return _check_bindings(
        bindings,
        expected_claims=OFFER_CLAIMS,
        revision=revision,
        client=client,
        settings_obj=settings_obj,
    )


__all__ = [
    "ACTION_REQUIRED_CLAIMS",
    "CLAIM_CANONICAL_URLS",
    "CLAIM_CATALOG_CONFIGURATION",
    "CLAIM_CATALOG_ASSET_REFERENCES",
    "CLAIM_CURRENT_OFFER",
    "CLAIM_ORDER",
    "CLAIM_PAYMENT",
    "CLAIM_PUBLIC_POLICY_INPUTS",
    "CLAIM_SHIPMENT",
    "RevisionAuthorityBindingSet",
    "build_revision_authority_bindings",
    "check_fact_bindings",
    "check_offer_bindings",
]
