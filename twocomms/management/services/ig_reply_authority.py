"""Build a bounded, read-only authority snapshot for final IG reply checks.

The model response is never a source of truth here.  A ``price_quoted`` value
is only a candidate for the existing catalog/configuration resolver, and
proposed controls are never copied into ``authorized_actions``.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from itertools import islice
from typing import Iterable, Mapping
from urllib.parse import quote, urlsplit

from django.conf import settings
from django.utils import timezone

from management.services.ig_core_policy import ORDINARY_DISPATCH_WINDOW_DAYS
from management.services.ig_reply_truth import ReplyTruthContext


MAX_CATALOG_QUOTES = 8
MAX_SERVER_URLS = 16
MAX_TIMING_CLAIMS = 16
READINESS_GAP_CODES = frozenset({
    "catalog_quote_unverified",
    "checkout_url_not_current",
    "current_configuration_unverified",
    "current_episode_unavailable",
    "current_order_conflict",
    "server_url_invalid",
    "server_url_not_owned",
    "site_origin_unconfigured",
})
EVIDENCE_CODES = frozenset({
    "configured_server_url",
    "current_episode",
    "current_episode_manager_payment",
    "current_episode_order",
    "current_episode_paid_order",
    "current_episode_proposal",
    "current_episode_provider_payment",
    "current_configuration_price",
    "current_order_carrier_delivery",
    "current_order_preparing",
    "current_order_shipped",
    "exact_catalog_quote",
    "explicit_server_timing",
    "source_proved_conversation_amounts",
})


@dataclass(frozen=True)
class AuthorityReplyTruthContext(ReplyTruthContext):
    """Validator-compatible context with content-free build diagnostics."""

    readiness_gaps: tuple[str, ...] = ()
    evidence_codes: tuple[str, ...] = ()
    action_authority: str = "external_business_gates"


def _money(value) -> Decimal | None:
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return amount if amount >= 0 else None


def _append_unique(values: list, value) -> None:
    if value not in values:
        values.append(value)


def _configured_origin() -> tuple[str, str, int | None] | None:
    raw = str(getattr(settings, "SITE_BASE_URL", "") or "").strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return None
    return parsed.scheme.casefold(), parsed.hostname.casefold(), port


def _catalog_control_urls(control) -> tuple[str, ...]:
    """Resolve link candidates from published products, never from reply prose."""
    if not isinstance(control, Mapping) or not control.get("catalog_link"):
        return ()
    from management.services.ig_catalog_media import parse_product_ids
    from storefront.services.public_products import public_products_queryset

    identifiers = parse_product_ids(control.get("show_products") or control.get("product"))
    if not identifiers or len(identifiers) > MAX_CATALOG_QUOTES:
        return ()
    origin = _configured_origin()
    if origin is None:
        return ()
    base = str(getattr(settings, "SITE_BASE_URL", "") or "").rstrip("/")
    return tuple(
        f"{base}/product/{quote(slug, safe='-_.~')}/"
        for slug in public_products_queryset().filter(pk__in=identifiers).values_list("slug", flat=True)
    )


def _server_urls(
    values: Iterable[object],
    *,
    current_proposal=None,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Keep exact own-origin URLs and bind bearer checkout links to this offer."""
    origin = _configured_origin()
    if origin is None:
        return (), ("site_origin_unconfigured",), ()

    accepted: list[str] = []
    gaps: list[str] = []
    evidence: list[str] = []
    checkout_tokens: dict[str, str] = {}
    candidates: list[tuple[str, object]] = []
    for raw in islice(values or (), MAX_SERVER_URLS):
        value = str(raw or "").strip()
        if not value:
            continue
        try:
            parsed = urlsplit(value)
            candidate_origin = (
                parsed.scheme.casefold(),
                (parsed.hostname or "").casefold(),
                parsed.port,
            )
        except (TypeError, ValueError):
            _append_unique(gaps, "server_url_invalid")
            continue
        if (
            candidate_origin != origin
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            _append_unique(gaps, "server_url_not_owned")
            continue
        candidates.append((value, parsed))
        path_parts = [part for part in parsed.path.split("/") if part]
        if len(path_parts) == 3 and path_parts[:2] == ["offer", "a"]:
            checkout_tokens[value] = path_parts[2]

    valid_checkout_urls: set[str] = set()
    if checkout_tokens:
        from management.models import IgCheckoutAccessToken

        digests = {
            value: IgCheckoutAccessToken.digest(token)
            for value, token in checkout_tokens.items()
        }
        now = timezone.now()
        proposal_id = getattr(current_proposal, "pk", None)
        rows = IgCheckoutAccessToken.objects.filter(
            token_digest__in=digests.values(),
            proposal_id=proposal_id,
            revoked_at__isnull=True,
            expires_at__gt=now,
        ).values_list("token_digest", flat=True)
        valid_digests = set(rows)
        valid_checkout_urls = {
            value for value, digest in digests.items() if digest in valid_digests
        }

    for value, _parsed in candidates:
        if value in checkout_tokens and value not in valid_checkout_urls:
            _append_unique(gaps, "checkout_url_not_current")
            continue
        _append_unique(accepted, value)
        _append_unique(evidence, "configured_server_url")
    return tuple(accepted), tuple(gaps), tuple(evidence)


def _current_episode(client):
    episode_id = getattr(client, "current_commercial_episode_id", None)
    if not getattr(client, "pk", None) or not episode_id:
        return None
    from management.models import IgCommercialEpisode

    return (
        IgCommercialEpisode.objects.select_related(
            "deal__active_checkout_proposal",
            "deal__order",
            "intended_order",
            "order_attribution__order",
        )
        .filter(pk=episode_id, client_id=client.pk, open_slot=1)
        .first()
    )


def _episode_order(episode):
    candidates = []
    intended = getattr(episode, "intended_order", None)
    if intended is not None:
        candidates.append(intended)
    attribution = getattr(episode, "order_attribution", None)
    attributed = getattr(attribution, "order", None) if attribution else None
    if attributed is not None:
        candidates.append(attributed)
    deal = getattr(episode, "deal", None)
    deal_order = getattr(deal, "order", None) if deal else None
    if deal_order is not None:
        candidates.append(deal_order)
    ids = {row.pk for row in candidates if getattr(row, "pk", None)}
    if len(ids) > 1:
        return None, "current_order_conflict"
    return (candidates[0] if candidates else None), ""


def _payment_confirmed(client, episode, order) -> tuple[bool, str]:
    from management.services.bot_payment_truth import (
        CONFIRMED_ORDER_PAYMENT_STATUSES,
        current_manager_confirmation_review_q,
        verified_payment_deals,
    )

    deal_id = getattr(episode, "deal_id", None)
    if deal_id and verified_payment_deals(
        client.deals.filter(pk=deal_id)
    ).exists():
        return True, "current_episode_provider_payment"
    review_id = getattr(episode, "primary_payment_review_id", None)
    if review_id and client.payment_confirmation_reviews.filter(
        current_manager_confirmation_review_q(), pk=review_id
    ).exists():
        return True, "current_episode_manager_payment"
    if order is not None and str(order.payment_status or "") in (
        CONFIRMED_ORDER_PAYMENT_STATUSES
    ):
        return True, "current_episode_paid_order"
    return False, ""


def _current_proposal(episode):
    deal = getattr(episode, "deal", None)
    proposal = getattr(deal, "active_checkout_proposal", None) if deal else None
    if proposal is None or proposal.commercial_episode_id != episode.pk:
        return None
    from management.models import IgCheckoutProposal

    active = {
        IgCheckoutProposal.Status.READY,
        IgCheckoutProposal.Status.VIEWED,
        IgCheckoutProposal.Status.DETAILS_LOCKED,
        IgCheckoutProposal.Status.INVOICE_CREATED,
        IgCheckoutProposal.Status.MANAGER_REVIEW,
        IgCheckoutProposal.Status.PAID,
    }
    if proposal.status not in active:
        return None
    if proposal.status != IgCheckoutProposal.Status.PAID and proposal.expires_at <= timezone.now():
        return None
    return proposal


def _resolved_catalog_quote(client, candidate: Mapping[str, object] | None):
    if not isinstance(candidate, Mapping) or "price_quoted" not in candidate:
        return None
    from management.services.instagram_bot import _validated_price_quote

    catalog_candidate = dict(candidate)
    # ``price`` is the legacy negotiated-price alias.  Passing it through
    # would let `_validated_price_quote` consult conversational evidence before
    # catalog pricing, which is outside this factory's authority contract.
    catalog_candidate.pop("price", None)
    resolved = _validated_price_quote(client, catalog_candidate)
    if not isinstance(resolved, Mapping) or resolved.get("price_source") != "catalog":
        return None
    return resolved


def _current_configuration_pricing(client, control: Mapping[str, object] | None):
    """Resolve at most one published product/configuration without model prices."""
    if not getattr(client, "pk", None):
        return None, False
    candidate = dict(control) if isinstance(control, Mapping) else {}
    if candidate.get("_invalid"):
        return None, True
    from management.services.instagram_bot import (
        _checkout_selection_state,
        _control_option_values,
        _control_product_id,
    )
    from storefront.models import Product, ProductStatus

    supplied_product_values = [
        candidate.get(key)
        for key in ("product", "product_id")
        if key in candidate
    ]
    explicit_product = bool(supplied_product_values)
    if len(supplied_product_values) > 1 and len({
        str(value).strip() for value in supplied_product_values
    }) > 1:
        return None, True
    product_candidate = (
        {"product": supplied_product_values[0]} if explicit_product else {}
    )
    product_id = _control_product_id(product_candidate) if explicit_product else None
    if not product_id:
        if explicit_product:
            return None, True
        product_id = getattr(client, "current_product_id", None)
    try:
        product_id = int(product_id or 0)
    except (TypeError, ValueError):
        return None, bool(explicit_product)
    if not product_id:
        return None, False
    product = Product.objects.filter(
        pk=product_id,
        status=ProductStatus.PUBLISHED,
    ).first()
    if product is None:
        return None, True

    selection = _checkout_selection_state(client, product_id)
    explicit_variant = None
    variant_supplied = False
    for key in ("color_variant_id", "variant"):
        if key in candidate:
            variant_supplied = True
            explicit_variant = candidate.get(key)
            break
    if variant_supplied:
        if isinstance(explicit_variant, bool):
            return None, True
        try:
            variant_id = int(explicit_variant or 0)
        except (TypeError, ValueError):
            return None, True
        if variant_id <= 0:
            return None, True
    else:
        try:
            variant_id = int(selection.get("color_variant_id") or 0)
        except (TypeError, ValueError):
            return None, True

    proposed_options = _control_option_values(candidate)
    if proposed_options is None:
        return None, True
    persisted_options = selection.get("option_values")
    options = (
        dict(persisted_options)
        if isinstance(persisted_options, Mapping)
        else {}
    )
    options.update(proposed_options)
    fit = str(candidate.get("fit") or "").strip().casefold()
    if fit:
        if "fit" in proposed_options and proposed_options["fit"] != fit:
            return None, True
        options["fit"] = fit
    elif selection.get("fit_option_code") and "fit" not in options:
        options["fit"] = str(selection["fit_option_code"]).strip().casefold()

    from management.services.ig_catalog_pricing import resolve_product_pricing

    variants = list(
        product.color_variants.select_related("color").order_by("order", "id")
    )
    if variant_id and not any(variant.pk == variant_id for variant in variants):
        return None, True
    pricing = resolve_product_pricing(
        product,
        variants=variants,
        selected_variant_id=variant_id or None,
        option_values=options or None,
    )
    if pricing.get("minimum") is None or pricing.get("maximum") is None:
        return None, True
    return pricing, False


def _shipment_truth(order) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    if order is None:
        return "unknown", (), ()
    from management.models import IgOrderShipment
    from orders.fulfillment_truth import nova_poshta_delivery_confirmed_at

    tracking: list[str] = []
    if str(order.tracking_number or "").strip():
        tracking.append(str(order.tracking_number).strip())
    for value in IgOrderShipment.objects.filter(
        order_id=order.pk,
        source__in=(
            IgOrderShipment.Source.ORDER_FIELD,
            IgOrderShipment.Source.MANAGER_MANUAL,
        ),
    ).order_by("created_at", "id").values_list("tracking_number", flat=True)[:16]:
        value = str(value or "").strip()
        if value:
            _append_unique(tracking, value)
    if nova_poshta_delivery_confirmed_at(order) is not None:
        return "delivered", tuple(tracking), ("current_order_carrier_delivery",)
    if order.status == "ship" and tracking:
        return "shipped", tuple(tracking), ("current_order_shipped",)
    if order.status == "prep":
        # The Order label is "preparing for shipment".  It does not prove the
        # positive customer-facing claim "ready to ship".
        return "preparing", tuple(tracking), ("current_order_preparing",)
    return "unknown", tuple(tracking), ()


def _conversation_amounts(client):
    context = client.sales_context if isinstance(client.sales_context, dict) else {}
    stored = context.get("conversation_agreement")
    if not isinstance(stored, dict):
        return ()
    from management.models import InstagramBotSettings, InstagramBotMessage
    from management.services.instagram_bot import ingress_provider_namespace
    from management.services.ig_conversation_routes import conversation_route_reset_floor
    from management.services.ig_conversation_agreement import MAX_MESSAGES, _row, extract_conversation_agreement, read_conversation_agreement
    from management.services.ig_memory_producer import _namespaces, _source_allowed
    settings_row = InstagramBotSettings.objects.filter(pk=1).first()
    if settings_row is None:
        return ()
    captured = read_conversation_agreement(client,
        episode_id=client.current_commercial_episode_id,
        source_namespace=ingress_provider_namespace(settings_row),
        reset_floor=conversation_route_reset_floor(client.pk))
    agreement = captured.get("agreement") or {}
    if not agreement:
        return ()
    new_sources = list(InstagramBotMessage.objects.filter(client_id=client.pk,
        pk__gt=agreement["watermark_message_id"], status="done").order_by("pk")[:MAX_MESSAGES + 1])
    if new_sources:
        retained = captured.get("source_rows") or []
        if len(retained) + len(new_sources) > MAX_MESSAGES:
            return ()
        namespace = ingress_provider_namespace(settings_row)
        namespaces = _namespaces(new_sources)
        if any(source.sender_id != client.igsid or namespaces.get(source.pk) != namespace
                or not _source_allowed(source) or (source.provider_created_at or source.created_at) > timezone.now()
                for source in new_sources):
            return ()
        reproduced = extract_conversation_agreement([*retained,
            *[{**_row(source), "provider_namespace": namespaces[source.pk]} for source in new_sources]])
        if any(reproduced.get(key) != agreement.get(key) for key in ("items", "amounts", "shipping_payment")):
            return ()
    amounts = agreement.get("amounts") or {}
    if amounts.get("authority") not in {"conversation_agreement", "seller_instruction"}:
        return ()
    if any(reason in (agreement.get("uncertainty_reasons") or []) for reason in (
        "amount_arithmetic_mismatch", "multi_item_price_allocation_required", "conversation_price_allocation_required")):
        return ()
    ids = agreement.get("source_message_ids") or []
    result = []
    currency = amounts.get("currency")
    if currency not in {"UAH", "USD", "EUR"}:
        return ()
    for kind, key in (("merchandise", "merchandise_total"), ("delivery", "delivery_amount"), ("payable", "payable_total")):
        value = amounts.get(key)
        source_id = amounts.get("delivery_source_message_id") if kind == "delivery" else amounts.get("source_message_id")
        source_id = source_id or amounts.get("source_message_id")
        amount = _money(value) if value not in (None, "") else None
        if amount is not None and amount.is_finite() and type(source_id) is int and source_id in ids:
            result.append((kind, f"{amount:.2f}", currency, source_id, amounts["authority"]))
    return tuple(result)


def build_reply_truth_context(
    client,
    *,
    control: Mapping[str, object] | None = None,
    server_urls: Iterable[object] = (),
    catalog_quotes: Iterable[Mapping[str, object]] = (),
    server_timing_claims: Iterable[object] = (),
) -> ReplyTruthContext:
    """Return current-episode facts suitable for ``validate_reply_truth``.

    ``server_urls``, ``catalog_quotes`` and ``server_timing_claims`` must be
    application-produced inputs.  The factory never extracts them from model
    prose.  Actions remain owned by the existing business gates.
    """
    gaps: list[str] = []
    evidence: list[str] = []
    prices: list[Decimal] = []
    ranges: list[tuple[Decimal, Decimal]] = []
    discount_amounts: list[Decimal] = []
    sizes: list[str] = []
    fits: list[str] = []
    colors: list[str] = []
    currencies: list[str] = ["UAH"]

    episode = _current_episode(client)
    order = None
    proposal = None
    payment_confirmed = False
    order_created = False
    shipment_state = "unknown"
    tracking_refs: tuple[str, ...] = ()
    if episode is None:
        _append_unique(gaps, "current_episode_unavailable")
    else:
        _append_unique(evidence, "current_episode")
        order, order_gap = _episode_order(episode)
        if order_gap:
            _append_unique(gaps, order_gap)
        order_created = order is not None
        if order_created:
            _append_unique(evidence, "current_episode_order")
        payment_confirmed, payment_evidence = _payment_confirmed(
            client, episode, order
        )
        if payment_evidence:
            _append_unique(evidence, payment_evidence)
        shipment_state, tracking_refs, shipment_evidence = _shipment_truth(order)
        for code in shipment_evidence:
            _append_unique(evidence, code)

        proposal = _current_proposal(episode)
        if proposal is not None:
            _append_unique(evidence, "current_episode_proposal")
            currency = str(proposal.currency or "").strip().upper()
            if currency:
                currencies = [currency]
            for value in (
                proposal.catalog_total,
                proposal.quoted_total,
            ):
                amount = _money(value)
                if amount is not None and amount > 0:
                    _append_unique(prices, amount)
            discount = _money(proposal.negotiated_discount)
            if discount is not None and discount > 0:
                _append_unique(discount_amounts, discount)
            for item in proposal.items.all().order_by("position", "id")[:40]:
                for value in (
                    item.catalog_unit_price,
                    item.catalog_line_total,
                    item.quoted_unit_price,
                    item.quoted_line_total,
                ):
                    amount = _money(value)
                    if amount is not None and amount > 0:
                        _append_unique(prices, amount)
                for target, value in (
                    (sizes, item.size),
                    (fits, item.fit_code),
                    (colors, item.color_code),
                    (colors, item.color_label),
                ):
                    normalized = str(value or "").strip()
                    if normalized:
                        _append_unique(target, normalized)

    configuration_pricing, configuration_invalid = _current_configuration_pricing(
        client, control
    )
    if configuration_invalid:
        _append_unique(gaps, "current_configuration_unverified")
    elif configuration_pricing is not None:
        minimum = _money(configuration_pricing.get("minimum"))
        maximum = _money(configuration_pricing.get("maximum"))
        if minimum is not None and maximum is not None:
            if configuration_pricing.get("exact") and minimum > 0:
                _append_unique(prices, minimum)
            elif 0 < minimum < maximum:
                _append_unique(ranges, (minimum, maximum))
            _append_unique(evidence, "current_configuration_price")

    # A current published product/variant price is independent of whether the
    # customer has opened a commercial episode.  The candidate still has no
    # authority by itself: the existing exact resolver must reproduce it.
    quote_candidates: list[Mapping[str, object]] = []
    if isinstance(control, Mapping) and "price_quoted" in control:
        quote_candidates.append(control)
    quote_candidates.extend(islice(catalog_quotes or (), MAX_CATALOG_QUOTES))
    resolved_quote_amounts: list[Decimal] = []
    for candidate in quote_candidates[:MAX_CATALOG_QUOTES]:
        resolved = _resolved_catalog_quote(client, candidate)
        amount = _money(resolved.get("amount")) if resolved else None
        if amount is None or amount <= 0:
            _append_unique(gaps, "catalog_quote_unverified")
            continue
        _append_unique(prices, amount)
        _append_unique(resolved_quote_amounts, amount)
        _append_unique(evidence, "exact_catalog_quote")
        fit = str(resolved.get("fit_option_code") or "").strip()
        if fit:
            _append_unique(fits, fit)
    if len(resolved_quote_amounts) > 1:
        low, high = min(resolved_quote_amounts), max(resolved_quote_amounts)
        if low != high:
            ranges.append((low, high))

    authorized_urls, url_gaps, url_evidence = _server_urls(
        (*_catalog_control_urls(control), *islice(server_urls or (), MAX_SERVER_URLS)),
        current_proposal=proposal,
    )
    for code in url_gaps:
        _append_unique(gaps, code)
    for code in url_evidence:
        _append_unique(evidence, code)

    timing: list[str] = []
    for value in islice(server_timing_claims or (), MAX_TIMING_CLAIMS):
        normalized = " ".join(str(value or "").split())[:160]
        if normalized:
            _append_unique(timing, normalized)
    if timing:
        _append_unique(evidence, "explicit_server_timing")
    conversation_amounts = _conversation_amounts(client)
    if conversation_amounts:
        _append_unique(evidence, "source_proved_conversation_amounts")

    return AuthorityReplyTruthContext(
        authorized_prices=tuple(prices),
        authorized_price_ranges=tuple(ranges),
        allowed_currency_codes=tuple(currencies),
        authorized_urls=authorized_urls,
        authorized_discount_amounts=tuple(discount_amounts),
        payment_confirmed=payment_confirmed,
        order_created=order_created,
        shipment_state=shipment_state,
        known_tracking_refs=tracking_refs,
        approved_timing_claims=tuple(timing),
        explicitly_qualified_standard_dispatch_days=(
            ORDINARY_DISPATCH_WINDOW_DAYS
        ),
        allowed_sizes=tuple(sizes),
        allowed_fits=tuple(fits),
        allowed_colors=tuple(colors),
        authorized_actions=(),
        conversation_amounts=conversation_amounts,
        readiness_gaps=tuple(gaps),
        evidence_codes=tuple(evidence),
    )


__all__ = [
    "AuthorityReplyTruthContext",
    "EVIDENCE_CODES",
    "READINESS_GAP_CODES",
    "build_reply_truth_context",
]
