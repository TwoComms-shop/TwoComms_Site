from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone


MAX_CHECKOUT_ITEMS = 12
MAX_CHECKOUT_QUANTITY = 50
MAX_CHECKOUT_VALUE = Decimal("1000000.00")


class CheckoutConfigurationError(ValueError):
    def __init__(
        self,
        code,
        message=None,
        *,
        missing_fields=None,
        item_index=None,
        reason="",
    ):
        super().__init__(message or code)
        self.code = code
        self.missing_fields = set(missing_fields or ())
        self.item_index = item_index
        self.reason = str(reason or "")[:128]


@dataclass(frozen=True)
class ValidatedCheckoutItem:
    product: object
    color_variant: object | None
    quantity: int
    size: str
    fit_code: str
    fit_label: str
    option_values: dict
    option_labels: dict
    catalog_unit_price: Decimal
    catalog_line_total: Decimal
    product_title: str
    sku: str
    image_url: str
    color_code: str
    color_label: str
    evidence_message_ids: tuple[int, ...]

    def digest_payload(self):
        return {
            "product_id": self.product.pk,
            "color_variant_id": self.color_variant.pk if self.color_variant else None,
            "quantity": self.quantity,
            "size": self.size,
            "fit_code": self.fit_code,
            "option_values": self.option_values,
            "catalog_unit_price": str(self.catalog_unit_price),
            "catalog_line_total": str(self.catalog_line_total),
        }


@dataclass(frozen=True)
class ValidatedQuote:
    items: tuple[ValidatedCheckoutItem, ...]
    catalog_total: Decimal
    negotiated_discount: Decimal
    quoted_total: Decimal
    requested_payment_amount: Decimal
    pay_type: str
    evidence_message_ids: tuple[int, ...]
    digest: str


def _money(value, *, code="invalid_amount"):
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        raise CheckoutConfigurationError(code)
    if amount <= 0 or amount > MAX_CHECKOUT_VALUE:
        raise CheckoutConfigurationError(code)
    return amount


def _normalize_message_ids(values):
    result = []
    for value in values or ():
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0 and number not in result:
            result.append(number)
    return tuple(result[:40])


def _normalize_options(raw):
    if raw in (None, ""):
        return {}
    if not isinstance(raw, dict) or len(raw) > 12:
        raise CheckoutConfigurationError("invalid_options")
    result = {}
    for key, value in raw.items():
        key = str(key or "").strip()[:100]
        value = str(value or "").strip()[:100]
        if key and value:
            result[key] = value
    return result


def _snapshot_image_url(product, variant):
    image = None
    if variant is not None:
        row = variant.images.order_by("order", "id").first()
        image = getattr(row, "image", None) if row is not None else None
    if not image:
        image = getattr(product, "display_image", None)
    try:
        return str(image.url)[:600] if image else ""
    except (AttributeError, ValueError):
        return ""


def _validate_negotiated_evidence(*, client, total, message_ids, items=()):
    from management.models import InstagramBotMessage

    rows = list(
        InstagramBotMessage.objects.filter(
            client=client,
            pk__in=message_ids,
        ).order_by("id")
    )
    if len(rows) != len(set(message_ids)):
        raise CheckoutConfigurationError("invalid_price_evidence")
    amount_re = re.compile(r"(?<!\d)(\d{2,6}(?:[.,]\d{1,2})?)\s*(?:грн|uah|₴)", re.I)
    acceptance_re = re.compile(
        r"\b(так|да|ок|добре|хорошо|домов\w*|погодж\w*|соглас\w*|оформл\w*)\b",
        re.I,
    )
    order_total_re = re.compile(
        r"\b(сума|сумма|разом|итого|всього|всего|total)\b",
        re.I,
    )
    unit_price_re = re.compile(
        r"\b(кожн\w*|кажд\w*|за\s+(?:одну|один|1|шт\.?|штук\w*|"
        r"одиниц\w*)|за\s+штуку|per\s+(?:item|unit)|each)\b",
        re.I,
    )
    total_quantity = sum(int(item.quantity or 0) for item in items)
    # A generated assistant message is not commercial authorization. Only a
    # human/operator-originated offer can support a negotiated total.
    seller_roles = {"manager", "human_manager", "operator", "admin"}
    customer_roles = {"user", "customer", "client"}
    offer_index = None
    for index, row in enumerate(rows):
        if str(row.role or "").casefold() not in seller_roles:
            continue
        amounts = []
        for raw in amount_re.findall(row.text or ""):
            try:
                amounts.append(Decimal(raw.replace(",", ".")).quantize(Decimal("0.01")))
            except InvalidOperation:
                continue
        text = row.text or ""
        explicit_total = total in amounts and (
            total_quantity <= 1 or order_total_re.search(text)
        )
        explicit_unit_total = bool(
            len(items) == 1
            and total_quantity > 1
            and unit_price_re.search(text)
            and any(amount * total_quantity == total for amount in amounts)
        )
        if explicit_total or explicit_unit_total:
            offer_index = index
    accepted = any(
        index > (offer_index if offer_index is not None else len(rows))
        and str(row.role or "").casefold() in customer_roles
        and acceptance_re.search(row.text or "")
        for index, row in enumerate(rows)
    )
    if offer_index is None or not accepted:
        raise CheckoutConfigurationError("invalid_price_evidence")


def tracked_stock_shortfall(variant, quantity: int) -> bool:
    """Чи справді не вистачає товару — за тим самим правилом, що й на сайті.

    `ProductColorVariant.stock` у цьому проєкті **не** є джерелом істини про
    наявність: на проді `stock > 0` лише в 1 варіанта з 81, тоді як сайт продає
    усі 71 опублікований товар, бо речі відшиваються під замовлення. Ні
    `storefront` кошик, ні `variant_allows_purchase` це поле не читають; каталог
    для бота прямо пише «під замовлення», коли воно нульове.

    Єдиним місцем, де нуль трактувався як заборона, був IG-чекаут. Наслідок був
    видимий у переписці 02.08: клієнт надсилав посилання на реальний
    опублікований товар і чув «Выбранный вариант сейчас недоступен в нужном
    количестве» — чотири рази підряд.

    Тому нуль означає «облік по цьому варіанту не ведеться», а не «немає».
    Додатне значення менеджер веде свідомо, і його ми поважаємо: коли на складі
    2, а просять 3, це справжня недостача.
    """
    try:
        tracked = int(getattr(variant, "stock", 0) or 0)
    except (TypeError, ValueError):
        return False
    return 0 < tracked < int(quantity or 1)


def validate_checkout_items(
    *,
    client,
    item_specs,
    evidence=None,
    pay_type="online_full",
    negotiated_total=None,
    requested_payment_amount=None,
    allow_promo=False,
):
    from product_catalog.services import effective_cart_unit_price, variant_allows_purchase
    from product_catalog.size_grid_services import (
        normalize_size_value,
        resolve_effective_sizes,
        resolve_option_size_grid,
    )
    from productcolors.models import ProductColorVariant
    from storefront.models import Product, ProductFitOption, ProductStatus
    from storefront.services.size_guides import resolve_product_sizes

    if not client or not getattr(client, "pk", None):
        raise CheckoutConfigurationError("invalid_client")
    if not isinstance(item_specs, (list, tuple)) or not item_specs:
        raise CheckoutConfigurationError("invalid_items")
    if len(item_specs) > MAX_CHECKOUT_ITEMS:
        raise CheckoutConfigurationError("too_many_items")

    evidence = evidence if isinstance(evidence, dict) else {}
    evidence_ids = _normalize_message_ids(evidence.get("message_ids"))
    normalized = []
    identities = set()
    total_quantity = 0
    missing_fields = set()

    for index, raw in enumerate(item_specs):
        if not isinstance(raw, dict):
            raise CheckoutConfigurationError("invalid_items", item_index=index)
        try:
            product_id = int(raw.get("product_id"))
            quantity = int(raw.get("qty", raw.get("quantity", 1)))
        except (TypeError, ValueError):
            raise CheckoutConfigurationError("invalid_items", item_index=index)
        if product_id <= 0 or quantity <= 0 or quantity > MAX_CHECKOUT_QUANTITY:
            raise CheckoutConfigurationError("invalid_quantity", item_index=index)
        total_quantity += quantity
        if total_quantity > MAX_CHECKOUT_QUANTITY:
            raise CheckoutConfigurationError("aggregate_quantity_limit")

        product = Product.objects.filter(pk=product_id).first()
        if product is None:
            raise CheckoutConfigurationError("invalid_product", item_index=index)
        if product.status != ProductStatus.PUBLISHED:
            raise CheckoutConfigurationError("unpublished_product", item_index=index)

        size = str(raw.get("size") or "").strip().upper()[:32]
        fit_code = str(
            raw.get("fit_option_code") or raw.get("fit") or ""
        ).strip().lower()[:64]
        color_value = raw.get("color_variant_id", raw.get("variant_id"))
        active_fits = list(
            ProductFitOption.objects.filter(product=product, is_active=True).order_by("order", "id")
        )
        item_missing = set()

        fit = None
        if active_fits and not fit_code:
            item_missing.add("fit")
        elif fit_code:
            fit = next((row for row in active_fits if row.code == fit_code), None)
            if fit is None:
                raise CheckoutConfigurationError("invalid_fit", item_index=index)
        elif not active_fits and str(raw.get("fit_option_code") or raw.get("fit") or "").strip():
            raise CheckoutConfigurationError("invalid_fit", item_index=index)

        option_values = _normalize_options(raw.get("option_values"))
        option_labels = _normalize_options(raw.get("option_labels"))
        if fit_code and fit is not None:
            option_values["fit"] = fit_code
            option_labels["fit"] = fit.label

        color_variants = list(
            ProductColorVariant.objects
            .select_related("color")
            .filter(product=product)
            .order_by("order", "id")
        )
        variant = None
        if color_value not in (None, "", False, 0, "0"):
            try:
                variant_id = int(color_value)
            except (TypeError, ValueError):
                raise CheckoutConfigurationError("invalid_color", item_index=index)
            variant = next((row for row in color_variants if row.pk == variant_id), None)
            if variant is None:
                raise CheckoutConfigurationError("invalid_color", item_index=index)
            if tracked_stock_shortfall(variant, quantity):
                raise CheckoutConfigurationError(
                    "insufficient_stock",
                    item_index=index,
                    reason="tracked_stock_shortfall",
                )
        elif color_variants:
            sellable_variants = [
                row
                for row in color_variants
                if not tracked_stock_shortfall(row, quantity)
                and variant_allows_purchase(
                    product,
                    row,
                    fit_code=fit_code,
                    size=size,
                    option_values=option_values,
                )
            ]
            if not sellable_variants:
                raise CheckoutConfigurationError("unavailable_selection", item_index=index)
            if len(sellable_variants) == 1:
                variant = sellable_variants[0]
            else:
                item_missing.add("color")

        # ProductCatalog option axes are commercial facts, not display defaults.  The
        # public context intentionally chooses a first enabled option for
        # merchandising, but assisted checkout must reject a missing
        # multi-choice axis instead of silently pricing that default.
        try:
            from product_catalog.services import product_option_context

            option_context = product_option_context(
                product,
                variant=variant,
                option_values=option_values,
            )
        except Exception:
            # The option graph is part of the commercial price contract. A
            # transient catalog failure must not become a base-price quote.
            raise CheckoutConfigurationError(
                "configuration_unavailable",
                item_index=index,
            )
        known_option_axes = {
            str(axis.get("code") or "").strip().lower(): axis
            for axis in option_context.get("axes") or []
            if str(axis.get("code") or "").strip()
        }
        for key, value in option_values.items():
            axis = known_option_axes.get(key)
            if axis is None:
                raise CheckoutConfigurationError("invalid_options", item_index=index)
            choice = next(
                (
                    choice for choice in axis.get("choices") or []
                    if choice.get("is_enabled")
                    and str(choice.get("code") or "").strip().lower() == value
                ),
                None,
            )
            if choice is None:
                raise CheckoutConfigurationError("invalid_options", item_index=index)
            option_labels.setdefault(
                key,
                str(choice.get("label") or choice.get("code") or value),
            )
        for axis_code, axis in known_option_axes.items():
            if axis_code == "fit":
                continue
            enabled_choices = [
                choice for choice in axis.get("choices") or []
                if choice.get("is_enabled")
            ]
            if not enabled_choices:
                item_missing.add(f"option:{axis_code}")
            elif len(enabled_choices) == 1 and axis_code not in option_values:
                only_choice = enabled_choices[0]
                option_values[axis_code] = str(
                    only_choice.get("code") or ""
                ).strip().lower()
                option_labels.setdefault(
                    axis_code,
                    str(only_choice.get("label") or option_values[axis_code]),
                )
            elif (
                axis_code not in option_values
                and not axis.get("fixed_choice")
                and len(enabled_choices) > 1
            ):
                item_missing.add(f"option:{axis_code}")

        option_key = f"fit={fit_code}" if fit_code else ""
        has_effective_grid = bool(
            option_key
            and resolve_option_size_grid(product, option_key, variant=variant)
        )
        if has_effective_grid:
            allowed_sizes = {
                normalize_size_value(row.get("size"))
                for row in resolve_effective_sizes(
                    product,
                    option_key,
                    variant=variant,
                )
                if row.get("is_enabled", True) and normalize_size_value(row.get("size"))
            }
            if not allowed_sizes:
                raise CheckoutConfigurationError("unavailable_selection", item_index=index)
        else:
            allowed_sizes = {
                normalize_size_value(value)
                for value in resolve_product_sizes(product)
                if normalize_size_value(value)
            }
        if allowed_sizes and not size:
            item_missing.add("size")
        if item_missing:
            missing_fields.update(item_missing)
            continue
        if allowed_sizes and normalize_size_value(size) not in allowed_sizes:
            raise CheckoutConfigurationError("invalid_size", item_index=index)

        if not variant_allows_purchase(
            product,
            variant,
            fit_code=fit_code,
            size=size,
            option_values=option_values,
        ):
            raise CheckoutConfigurationError("unavailable_selection", item_index=index)

        try:
            unit_price = Decimal(str(effective_cart_unit_price(
                product,
                variant,
                fit_code=fit_code,
                option_values=option_values,
            ))).quantize(Decimal("0.01"))
        except (InvalidOperation, TypeError, ValueError):
            raise CheckoutConfigurationError("invalid_catalog_price", item_index=index)
        if unit_price <= 0:
            raise CheckoutConfigurationError("invalid_catalog_price", item_index=index)
        line_total = unit_price * quantity
        identity = (
            product.pk,
            variant.pk if variant else 0,
            size,
            fit_code,
            json.dumps(option_values, ensure_ascii=True, sort_keys=True),
        )
        if identity in identities:
            raise CheckoutConfigurationError("duplicate_items", item_index=index)
        identities.add(identity)

        color = getattr(variant, "color", None) if variant else None
        normalized.append(ValidatedCheckoutItem(
            product=product,
            color_variant=variant,
            quantity=quantity,
            size=size,
            fit_code=fit_code,
            fit_label=(fit.label if fit else ""),
            option_values=option_values,
            option_labels=option_labels,
            catalog_unit_price=unit_price,
            catalog_line_total=line_total,
            product_title=str(product.title or "")[:255],
            sku=str(getattr(variant, "sku", "") or f"TC-{product.pk}")[:128],
            image_url=_snapshot_image_url(product, variant),
            color_code=str(getattr(color, "primary_hex", "") or "")[:64],
            color_label=str(getattr(color, "name", "") or "")[:100],
            evidence_message_ids=evidence_ids,
        ))

    if missing_fields:
        raise CheckoutConfigurationError(
            "missing_configuration",
            missing_fields=missing_fields,
        )

    catalog_total = sum((item.catalog_line_total for item in normalized), Decimal("0.00"))
    if catalog_total <= 0 or catalog_total > MAX_CHECKOUT_VALUE:
        raise CheckoutConfigurationError("invalid_catalog_total")
    quoted_total = catalog_total
    if negotiated_total is not None:
        quoted_total = _money(negotiated_total, code="invalid_negotiated_total")
        if quoted_total > catalog_total:
            raise CheckoutConfigurationError("invalid_negotiated_total")
        if quoted_total != catalog_total:
            if not evidence_ids:
                raise CheckoutConfigurationError("missing_price_evidence")
            _validate_negotiated_evidence(
                client=client,
                total=quoted_total,
                message_ids=evidence_ids,
                items=normalized,
            )

    normalized_pay_type = str(pay_type or "online_full").strip().lower()
    if normalized_pay_type in {"full", "online_full"}:
        normalized_pay_type = "online_full"
    elif normalized_pay_type in {"prepay", "prepayment"}:
        normalized_pay_type = "prepayment"
    else:
        raise CheckoutConfigurationError("invalid_pay_type")
    payment_amount = (
        quoted_total
        if normalized_pay_type == "online_full" and requested_payment_amount is None
        else _money(requested_payment_amount, code="invalid_payment_amount")
    )
    if payment_amount > quoted_total:
        raise CheckoutConfigurationError("payment_amount_exceeds_total")
    if normalized_pay_type == "prepayment" and not evidence_ids:
        raise CheckoutConfigurationError("missing_payment_evidence")

    digest_payload = {
        "items": [item.digest_payload() for item in normalized],
        "catalog_total": str(catalog_total),
        "quoted_total": str(quoted_total),
        "requested_payment_amount": str(payment_amount),
        "pay_type": normalized_pay_type,
        "allow_promo": bool(allow_promo),
        "evidence_message_ids": evidence_ids,
    }
    digest = hashlib.sha256(
        json.dumps(
            digest_payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return ValidatedQuote(
        items=tuple(normalized),
        catalog_total=catalog_total,
        negotiated_discount=catalog_total - quoted_total,
        quoted_total=quoted_total,
        requested_payment_amount=payment_amount,
        pay_type=normalized_pay_type,
        evidence_message_ids=evidence_ids,
        digest=digest,
    )


def _deal_item_snapshot(item):
    return {
        "product_id": item.product.pk,
        "color_variant_id": item.color_variant.pk if item.color_variant else None,
        "title": item.product_title,
        "size": item.size,
        "fit_option_code": item.fit_code,
        "fit_option_label": item.fit_label,
        "option_values": item.option_values,
        "option_labels": item.option_labels,
        "qty": item.quantity,
        "unit_price": str(item.catalog_unit_price),
        "line_total": str(item.catalog_line_total),
        "price_source": (
            "catalog_with_order_discount" if item.evidence_message_ids else "catalog"
        ),
        "price_evidence_message_ids": list(item.evidence_message_ids),
    }


def _revision_snapshot(quote):
    return {
        "items": [_deal_item_snapshot(item) for item in quote.items],
        "catalog_total": str(quote.catalog_total),
        "negotiated_discount": str(quote.negotiated_discount),
        "quoted_total": str(quote.quoted_total),
        "requested_payment_amount": str(quote.requested_payment_amount),
        "pay_type": quote.pay_type,
        "digest": quote.digest,
    }


def _sync_deal_and_episode(*, deal, quote):
    from management.models import IgDeal, IgDealItem
    from management.services.ig_commercial_episodes import ensure_episode_for_deal

    if deal.invoice_id or deal.invoice_url:
        raise CheckoutConfigurationError("legacy_invoice_exists")
    deal.items.all().delete()
    deal_rows = []
    for item in quote.items:
        snapshot = _deal_item_snapshot(item)
        deal_rows.append(IgDealItem(
            deal=deal,
            product=item.product,
            color_variant=item.color_variant,
            title=item.product_title,
            size=item.size,
            fit_option_code=item.fit_code,
            fit_option_label=item.fit_label,
            option_values=item.option_values,
            option_labels=item.option_labels,
            qty=item.quantity,
            unit_price=item.catalog_unit_price,
            line_total=item.catalog_line_total,
            price_source=snapshot["price_source"],
            price_evidence_message_ids=list(item.evidence_message_ids),
        ))
    IgDealItem.objects.bulk_create(deal_rows)
    deal.pay_type = (
        IgDeal.PayType.PREPAYMENT
        if quote.pay_type == "prepayment"
        else IgDeal.PayType.ONLINE_FULL
    )
    deal.amount = quote.quoted_total
    deal.currency = "UAH"
    deal.requested_payment_amount = quote.requested_payment_amount
    deal.requested_payment_evidence_ids = list(quote.evidence_message_ids)
    deal.status = IgDeal.Status.QUOTED
    deal.save(update_fields=[
        "pay_type", "amount", "currency", "requested_payment_amount",
        "requested_payment_evidence_ids", "status", "updated_at",
    ])
    episode = ensure_episode_for_deal(deal)
    episode.product_snapshot = [_deal_item_snapshot(item) for item in quote.items]
    episode.price_snapshot = {
        "catalog_total": str(quote.catalog_total),
        "negotiated_discount": str(quote.negotiated_discount),
        "negotiated_total": str(quote.quoted_total),
        "requested_payment_amount": str(quote.requested_payment_amount),
        "currency": "UAH",
        "proposal_digest": quote.digest,
    }
    episode.save(update_fields=["product_snapshot", "price_snapshot", "updated_at"])
    return episode


def _replace_proposal_items(*, proposal, quote):
    from management.models import IgCheckoutProposalItem

    # A revision replaces the current item rows.  Release the old active
    # allocation first; the reservation keeps its audit row while SET_NULL on
    # ``item`` allows the protected current snapshot to be replaced safely.
    from management.services.ig_inventory import release_proposal_inventory

    release_proposal_inventory(proposal, reason=f"proposal_revision:{proposal.revision}")
    proposal.items.all().delete()
    rows = []
    for position, item in enumerate(quote.items):
        rows.append(IgCheckoutProposalItem(
            proposal=proposal,
            product=item.product,
            color_variant=item.color_variant,
            product_title=item.product_title,
            sku=item.sku,
            image_url=item.image_url,
            color_code=item.color_code,
            color_label=item.color_label,
            size=item.size,
            fit_code=item.fit_code,
            fit_label=item.fit_label,
            option_values=item.option_values,
            option_labels=item.option_labels,
            quantity=item.quantity,
            catalog_unit_price=item.catalog_unit_price,
            catalog_line_total=item.catalog_line_total,
            quoted_unit_price=item.catalog_unit_price,
            quoted_line_total=item.catalog_line_total,
            price_source=(
                "catalog_with_order_discount"
                if quote.negotiated_discount > 0
                else "catalog"
            ),
            evidence_message_ids=list(item.evidence_message_ids),
            position=position,
        ))
    IgCheckoutProposalItem.objects.bulk_create(rows)


@transaction.atomic
def create_or_update_proposal(
    *,
    client,
    pay_type,
    item_specs,
    negotiated_total=None,
    requested_payment_amount=None,
    evidence=None,
    allow_promo=False,
    locale=None,
    deal=None,
):
    from management.models import (
        IgCheckoutProposal,
        IgCheckoutRevision,
        IgClient,
        IgDeal,
    )

    locked_client = IgClient.objects.select_for_update().get(pk=client.pk)
    locale_code = str(locale or getattr(locked_client, "language", "") or "uk").lower().replace("_", "-").split("-", 1)[0]
    if locale_code not in {"uk", "ru", "en"}:
        locale_code = "uk"
    from orders.checkout_series import assisted_checkout_v2_new_proposal_enabled

    new_v2_canary = assisted_checkout_v2_new_proposal_enabled(locked_client.pk)
    custom_print_requested = bool(
        any(
            isinstance(row, dict)
            and row.get("custom_print_lead_id") not in (None, "", 0, "0")
            for row in item_specs or ()
        )
    )
    quote = validate_checkout_items(
        client=locked_client,
        item_specs=item_specs,
        evidence=evidence,
        # V2 payment choice is made from persisted policy on the first browser
        # submit. Model/manager-supplied pay_type cannot pre-authorize 200+COD.
        pay_type="online_full" if new_v2_canary else pay_type,
        negotiated_total=negotiated_total,
        requested_payment_amount=(
            None if new_v2_canary else requested_payment_amount
        ),
        allow_promo=allow_promo,
    )
    if deal is not None:
        deal = (
            IgDeal.objects.select_for_update()
            .filter(pk=deal.pk, client=locked_client)
            .first()
        )
        if deal is None:
            raise CheckoutConfigurationError("invalid_deal")
    else:
        deal = (
            IgDeal.objects.select_for_update()
            .filter(client=locked_client, active_checkout_proposal__isnull=False)
            .order_by("-id")
            .first()
        )
    proposal_id = deal.active_checkout_proposal_id if deal is not None else None
    proposal = None
    if proposal_id:
        proposal = IgCheckoutProposal.objects.select_for_update().get(pk=proposal_id)
    activate_v2 = bool(
        proposal.assisted_checkout_v2 if proposal is not None else new_v2_canary
    )
    if proposal is not None and not activate_v2 and new_v2_canary:
        # Canary assignment applies only to a newly-created proposal. An
        # existing legacy proposal retains its exact original payment inputs.
        quote = validate_checkout_items(
            client=locked_client,
            item_specs=item_specs,
            evidence=evidence,
            pay_type=pay_type,
            negotiated_total=negotiated_total,
            requested_payment_amount=requested_payment_amount,
            allow_promo=allow_promo,
        )
    if activate_v2 and quote.pay_type != "online_full":
        quote = validate_checkout_items(
            client=locked_client,
            item_specs=item_specs,
            evidence=evidence,
            pay_type="online_full",
            negotiated_total=negotiated_total,
            requested_payment_amount=None,
            allow_promo=allow_promo,
        )
    payment_policy = None
    if activate_v2:
        from management.services.ig_checkout_policy import resolve_payment_policy

        payment_policy = resolve_payment_policy(
            client=locked_client,
            evidence_message_ids=quote.evidence_message_ids,
            custom_print_full_only=(
                custom_print_requested
                or bool(getattr(proposal, "custom_print_full_only", False))
            ),
        )
    if proposal is not None:
        local_terminal = {}
        if proposal.payment_attempt_id:
            from orders.models import PaymentAttempt

            locked_attempt = PaymentAttempt.objects.select_for_update().get(
                pk=proposal.payment_attempt_id
            )
            local_terminal = dict(
                (locked_attempt.event_state or {}).get("local_terminalization") or {}
            )
        if (
            proposal.status in {
                IgCheckoutProposal.Status.EXPIRED,
                IgCheckoutProposal.Status.CANCELLED,
            }
            and local_terminal
        ):
            # Repair a crash/race that committed the local terminal proposal but
            # left the fast active pointer behind. The historical deal/attempt/
            # invoice remains immutable; this request starts a fresh generation.
            if deal.active_checkout_proposal_id == proposal.pk:
                deal.active_checkout_proposal = None
                deal.save(update_fields=["active_checkout_proposal", "updated_at"])
            proposal = None
            deal = None
        elif proposal.is_expired:
            if proposal.status not in {
                IgCheckoutProposal.Status.READY,
                IgCheckoutProposal.Status.VIEWED,
                IgCheckoutProposal.Status.DETAILS_LOCKED,
                IgCheckoutProposal.Status.EXPIRED,
            } or proposal.payment_attempt_id:
                raise CheckoutConfigurationError("proposal_expired")
            if proposal.status != IgCheckoutProposal.Status.EXPIRED:
                proposal.status = IgCheckoutProposal.Status.EXPIRED
                proposal.save(update_fields=["status", "updated_at"])
            deal.active_checkout_proposal = None
            deal.save(update_fields=["active_checkout_proposal", "updated_at"])
            proposal = None
        elif proposal.items_digest == quote.digest and proposal.allow_promo == bool(allow_promo):
            policy_fields = []
            if activate_v2 and payment_policy is not None:
                for field_name, value in (
                    ("payment_policy", payment_policy.policy),
                    (
                        "payment_policy_evidence_message_id",
                        payment_policy.evidence_message_id,
                    ),
                    ("payment_policy_evidence_kind", payment_policy.evidence_kind),
                    (
                        "payment_policy_evidence_revision",
                        payment_policy.evidence_revision,
                    ),
                    (
                        "payment_policy_evidence_digest",
                        payment_policy.evidence_digest,
                    ),
                    (
                        "custom_print_full_only",
                        payment_policy.custom_print_full_only,
                    ),
                ):
                    if getattr(proposal, field_name) != value:
                        setattr(proposal, field_name, value)
                        policy_fields.append(field_name)
            if proposal.locale != locale_code and proposal.status in {
                IgCheckoutProposal.Status.READY,
                IgCheckoutProposal.Status.VIEWED,
            }:
                proposal.locale = locale_code
                policy_fields.append("locale")
            if policy_fields:
                proposal.save(update_fields=[*policy_fields, "updated_at"])
            from management.services.bot_followups import schedule_proposal_expiry_event

            schedule_proposal_expiry_event(proposal)
            return proposal
        if proposal is not None and (
            proposal.status not in {
                IgCheckoutProposal.Status.READY,
                IgCheckoutProposal.Status.VIEWED,
            }
            or proposal.payment_attempt_id
            or deal.invoice_id
            or deal.invoice_url
        ):
            raise CheckoutConfigurationError("proposal_locked")
    if deal is None:
        deal = IgDeal.objects.create(
            client=locked_client,
            status=IgDeal.Status.QUOTED,
            pay_type=(
                IgDeal.PayType.PREPAYMENT
                if quote.pay_type == "prepayment"
                else IgDeal.PayType.ONLINE_FULL
            ),
            amount=quote.quoted_total,
            requested_payment_amount=quote.requested_payment_amount,
        )

    episode = _sync_deal_and_episode(deal=deal, quote=quote)
    if proposal is None:
        proposal_values = {}
        if activate_v2 and payment_policy is not None:
            from management.services.ig_checkout_policy import PROPOSAL_TTL_SECONDS

            proposal_values = {
                "assisted_checkout_v2": True,
                "payment_policy": payment_policy.policy,
                "payment_policy_evidence_message_id": (
                    payment_policy.evidence_message_id
                ),
                "payment_policy_evidence_kind": payment_policy.evidence_kind,
                "payment_policy_evidence_revision": (
                    payment_policy.evidence_revision
                ),
                "payment_policy_evidence_digest": payment_policy.evidence_digest,
                "custom_print_full_only": payment_policy.custom_print_full_only,
                "expires_at": timezone.now()
                + timedelta(seconds=PROPOSAL_TTL_SECONDS),
            }
        proposal = IgCheckoutProposal.objects.create_current(
            deal=deal,
            commercial_episode=episode,
            catalog_total=quote.catalog_total,
            negotiated_discount=quote.negotiated_discount,
            quoted_total=quote.quoted_total,
            requested_payment_amount=quote.requested_payment_amount,
            pay_type=quote.pay_type,
            allow_promo=bool(allow_promo),
            items_digest=quote.digest,
            locale=locale_code,
            **proposal_values,
        )
        revision_number = 1
        revision_source = IgCheckoutRevision.Source.BOT_CREATE
    else:
        proposal.revision += 1
        proposal.catalog_total = quote.catalog_total
        proposal.negotiated_discount = quote.negotiated_discount
        proposal.quoted_total = quote.quoted_total
        proposal.requested_payment_amount = quote.requested_payment_amount
        proposal.pay_type = quote.pay_type
        proposal.allow_promo = bool(allow_promo)
        proposal.items_digest = quote.digest
        proposal.commercial_episode = episode
        proposal.locale = locale_code
        if activate_v2 and payment_policy is not None:
            proposal.payment_policy = payment_policy.policy
            proposal.payment_policy_evidence_message_id = (
                payment_policy.evidence_message_id
            )
            proposal.payment_policy_evidence_kind = payment_policy.evidence_kind
            proposal.payment_policy_evidence_revision = (
                payment_policy.evidence_revision
            )
            proposal.payment_policy_evidence_digest = payment_policy.evidence_digest
            proposal.custom_print_full_only = payment_policy.custom_print_full_only
        proposal.full_clean()
        proposal.save(update_fields=[
            "revision", "catalog_total", "negotiated_discount", "quoted_total",
            "requested_payment_amount", "pay_type", "allow_promo", "items_digest",
            "commercial_episode", "locale", "updated_at",
            *(
                [
                    "payment_policy",
                    "payment_policy_evidence_message",
                    "payment_policy_evidence_kind",
                    "payment_policy_evidence_revision",
                    "payment_policy_evidence_digest",
                    "custom_print_full_only",
                ]
                if activate_v2
                else []
            ),
        ])
        revision_number = proposal.revision
        revision_source = IgCheckoutRevision.Source.BOT_UPDATE

    _replace_proposal_items(proposal=proposal, quote=quote)
    from product_catalog.models import ProductInventoryPolicy
    if not proposal.assisted_checkout_v2 and ProductInventoryPolicy.objects.filter(
        product_id__in={item.product_id for item in proposal.items.all() if item.product_id},
    ).exists():
        try:
            from management.services.ig_inventory import (
                InventoryReservationError,
                reserve_proposal_inventory,
            )

            # Products without a policy remain legacy/made-to-order: they may
            # proceed without a warehouse reservation. Explicit warehouse or
            # catalog policies still fail closed in the resolver above.
            reserve_proposal_inventory(proposal)
        except InventoryReservationError as exc:
            code = (
                "insufficient_stock"
                if "insufficient" in exc.reason
                else "inventory_unavailable"
            )
            raise CheckoutConfigurationError(
                code,
                item_index=0,
                reason=exc.reason,
            )
    checkout_revision = IgCheckoutRevision.objects.create(
        proposal=proposal,
        revision=revision_number,
        digest=quote.digest,
        snapshot=_revision_snapshot(quote),
        source=revision_source,
        evidence_message_ids=list(quote.evidence_message_ids),
        source_watermark_message_id=max(quote.evidence_message_ids or (0,)),
    )
    try:
        # Optional diagram projection cannot poison the valid checkout's
        # transaction, including when a database statement fails.
        with transaction.atomic():
            from management.services.ig_journey_events import record_validated_checkout_revision

            record_validated_checkout_revision(checkout_revision)
    except Exception as exc:
        import logging

        logging.getLogger(__name__).warning(
            "journey_checkout_projection_failed revision_id=%s failure_type=%s",
            checkout_revision.pk, type(exc).__name__,
        )
    if locked_client.stage not in {
        IgClient.Stage.PAID,
        IgClient.Stage.ORDER_CREATED,
        IgClient.Stage.DONE,
    }:
        # Э3.2: прямая запись стадии обязана нести evidence в таймлайне клиента.
        # Раньше здесь стадия менялась без `IgClientStageEvent`, и на вопрос
        # «как клиент попал в checkout» ответа в CRM не было.
        from management.models import IgClientStageEvent

        stage_before = locked_client.stage
        locked_client.stage = IgClient.Stage.CHECKOUT
        locked_client.stage_updated_at = timezone.now()
        locked_client.save(update_fields=["stage", "stage_updated_at", "updated_at"])
        IgClientStageEvent.objects.create(
            client=locked_client,
            from_stage=stage_before or "",
            to_stage=IgClient.Stage.CHECKOUT,
            reason="checkout_proposal_created",
        )
    from management.services.bot_followups import schedule_proposal_expiry_event

    schedule_proposal_expiry_event(proposal)
    return proposal


def build_proposal(client, *, items, **kwargs):
    return create_or_update_proposal(
        client=client,
        item_specs=items,
        pay_type=kwargs.pop("pay_type", "online_full"),
        **kwargs,
    )
