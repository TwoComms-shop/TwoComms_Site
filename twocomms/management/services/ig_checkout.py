from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace, field
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone


MAX_CHECKOUT_ITEMS = 12
MAX_CHECKOUT_QUANTITY = 50
MAX_CHECKOUT_VALUE = Decimal("1000000.00")
_AMENDMENT_SUCCESSOR_ISSUER = object()


@dataclass(frozen=True)
class _RetiredAmendmentProposal:
    client_id: int
    deal_id: int
    proposal_id: int
    amendment_id: object
    source_hmac: str
    expires_at: object
    origin_status: str
    successor_id: int
    issuer: object


def validate_checkout_grant_revision_bridge(proposal, *, token, from_revision):
    """Read bridge through exact native revisions; never authorizes a mutation."""
    from management.models import IgCheckoutRevision, IgCheckoutCartAmendment
    from management.services.ig_checkout_cart_amendments import verify_native_transition, CheckoutAmendmentError
    if (type(from_revision) is not int or not 0 < proposal.revision - from_revision <= 64
            or token.proposal_id != proposal.pk or token.revoked_at or token.expires_at <= timezone.now()
            or proposal.client.privacy_erasure_started_at or proposal.client.hidden_at
            or proposal.deal.client_id != proposal.client_id
            or proposal.commercial_episode.client_id != proposal.client_id):
        return False
    revisions = {row.revision: row for row in IgCheckoutRevision.objects.filter(proposal=proposal,
        revision__gte=from_revision, revision__lte=proposal.revision).order_by("revision")[:65]}
    if len(revisions) != proposal.revision - from_revision + 1:
        return False
    receipts = list(IgCheckoutCartAmendment.objects.filter(proposal=proposal,
        from_revision__gte=from_revision, to_revision__lte=proposal.revision).select_related(
            "selection_transition__session", "token").order_by("from_revision")[:65])
    if len(receipts) != proposal.revision - from_revision:
        return False
    for number, receipt in enumerate(receipts, from_revision):
        try:
            body = verify_native_transition(receipt.selection_transition, historical=True)
        except (CheckoutAmendmentError, AttributeError):
            return False
        target = revisions.get(number + 1)
        native = (target.snapshot.get("source_cart_capture") or {}).get("fence", {}) if target else {}
        if (receipt.from_revision != number or receipt.to_revision != number + 1
                or receipt.client_id != proposal.client_id or receipt.token.proposal_id != proposal.pk
                or body.get("episode_id") != proposal.commercial_episode_id
                or body.get("pending_review") is not False or body.get("cancelled_scope") is not False
                or body.get("expected_digest") != revisions[number].digest
                or target.source != IgCheckoutRevision.Source.CHECKOUT_HOLDER
                or native.get("native_amendment_id") != str(receipt.pk)
                or native.get("native_source_hmac") != receipt.source_hmac):
            return False
    if revisions[proposal.revision].digest != proposal.items_digest:
        return False
    if proposal.payment_attempt_id:
        attempt = proposal.payment_attempt
        owner = (attempt.cart_snapshot or {}).get("source_cart_provenance", {}).get("owner", {})
        if (attempt.created < receipts[-1].created_at
                or (attempt.cart_snapshot or {}).get("proposal_id") != str(proposal.public_id)
                or owner.get("proposal_pk") != proposal.pk or owner.get("revision") != proposal.revision
                or owner.get("revision_id") != revisions[proposal.revision].pk):
            return False
    return True


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
    product: object | None
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
    price_parts: tuple = ()
    bundle_pricing: dict = field(default_factory=dict)
    assisted_line: dict = field(default_factory=dict)
    photo_asset_id: str | None = None
    photo_digest: str = ''

    def digest_payload(self):
        if self.product is None:
            from management.services.ig_revision_cart_binding import union_configuration, ASSISTED_SCHEMA
            row = assisted_item_snapshot(self)
            configured = union_configuration(row, schema=ASSISTED_SCHEMA)
            if configured is None:
                raise CheckoutConfigurationError('assisted_custom_configuration_invalid')
            return {**configured, 'catalog_unit_price': str(self.catalog_unit_price),
                'catalog_line_total': str(self.catalog_line_total)}
        payload = {
            "product_id": self.product.pk,
            "color_variant_id": self.color_variant.pk if self.color_variant else None,
            "quantity": self.quantity,
            "size": self.size,
            "fit_code": self.fit_code,
            "option_values": self.option_values,
            "catalog_unit_price": str(self.catalog_unit_price),
            "catalog_line_total": str(self.catalog_line_total),
        }
        if self.price_parts:
            payload["price_parts"] = list(self.price_parts)
        return payload


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
    assisted_commerce: dict = field(default_factory=dict)


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
    from management.services.ig_checkout_policy import (
        PROPOSAL_TTL_SECONDS, accepts_current_commercial_offer, affirmative_commercial_statement,
    )
    from management.services.ig_funnel_reset import current_message_floor

    rows = list(
        InstagramBotMessage.objects.filter(
            client=client,
            pk__in=message_ids,
            pk__gte=current_message_floor(client),
        ).order_by("id")
    )
    if len(rows) != len(set(message_ids)):
        raise CheckoutConfigurationError("invalid_price_evidence")
    amount_re = re.compile(r"(?<!\d)(\d{2,6}(?:[.,]\d{1,2})?)\s*(?:грн|uah|₴)", re.I)
    order_total_re = re.compile(
        r"\b(сума|сумма|разом|итого|всього|всего|total)\b",
        re.I,
    )
    unit_price_re = re.compile(
        r"\b(кожн\w*|кажд\w*|за\s+(?:одну|один|1|шт\.?|штук\w*|"
        r"одиниц\w*)|за\s+штуку|per\s+(?:item|unit)|each)\b",
        re.I,
    )
    excluded_amount_role = re.compile(r'(?:достав\w*|перевез\w*|пересил\w*|комісі\w*|комисси\w*|'
        r'зниж\w*|скид\w*|передоплат\w*|предоплат\w*|завдат\w*|депозит\w*|'
        r'повернен\w*|возврат\w*|сертифікат\w*|сертификат\w*|бонус\w*|балів|балл\w*|'
        r'доплат\w*|решт\w*|залиш\w*|остат\w*|бюджет\w*|budget|shipping|delivery|discount|deposit|commission|refund)', re.I)
    goods_amount_role = re.compile(r'(?:ціна|цена|вартіст\w*|стоимост\w*|футбол\w*|худі|худи|'
        r'світшот\w*|свитшот\w*|товар\w*|замовлен\w*|заказ\w*|пропон\w*|предлаг\w*|'
        r'домовлен\w*|согласован\w*|разом|итого|всього|всего|total|price|offer|each|кожн\w*|кажд\w*)', re.I)
    clause_boundary = re.compile(r'[,;!?\n]|(?<!\d)\.|\.(?!\d)')
    total_quantity = sum(int(item.quantity or 0) for item in items)
    # A generated assistant message is not commercial authorization. Only a
    # human/operator-originated offer can support a negotiated total.
    seller_roles = {"manager", "human_manager", "operator", "admin"}
    customer_roles = {"user", "customer", "client"}
    offer_index = None
    for index, row in enumerate(rows):
        if str(row.role or "").casefold() not in seller_roles:
            continue
        if (not affirmative_commercial_statement(row.text)
                or (timezone.now() - row.created_at).total_seconds() > PROPOSAL_TTL_SECONDS):
            continue
        text = row.text or ""
        if re.search(r'\b(?:хотів\w*|хотел\w*|бажан\w*|бажав\w*|бажає\w*|побажан\w*|wishlist|wish|хочу|хоче\w*)\b', text, re.I):
            continue
        quantity_mentions = re.findall(r'\b(\d{1,3}|одну|один|одна|дві|два|две|три|чотири|четыре)\s*'
            r'(?:шт\b|штук\w*|футбол\w*|худі\b|худи\b|світшот\w*|свитшот\w*)', text, re.I)
        quantity_words = {'одну': 1, 'один': 1, 'одна': 1, 'дві': 2, 'два': 2, 'две': 2,
            'три': 3, 'чотири': 4, 'четыре': 4}
        mentioned_quantities = [int(value) if value.isdigit() else quantity_words[value.casefold()]
            for value in quantity_mentions]
        if any(quantity != total_quantity and not (quantity == 1 and len(items) == 1
                and total_quantity > 1 and unit_price_re.search(text)) for quantity in mentioned_quantities):
            continue
        mentions = list(amount_re.finditer(text))
        goods_totals = set()
        unclassified_amount = False
        previous_end = 0
        for position, mention in enumerate(mentions):
            boundaries = list(clause_boundary.finditer(text, previous_end, mention.start()))
            start = boundaries[-1].end() if boundaries else previous_end
            prefix = text[start:mention.start()]
            next_start = mentions[position + 1].start() if position + 1 < len(mentions) else len(text)
            suffix = clause_boundary.split(text[mention.end():next_start], maxsplit=1)[0]
            previous_end = mention.end()
            # Currency is not its semantic role: delivery charges, discounts,
            # deposits and balances cannot authorize a merchandise quote.
            if excluded_amount_role.search(prefix) or re.match(
                    r'^\s*(?:за\s+)?' + excluded_amount_role.pattern, suffix, re.I):
                continue
            try:
                amount = Decimal(mention.group(1).replace(",", ".")).quantize(Decimal("0.01"))
            except InvalidOperation:
                continue
            local_role = prefix + ' ' + suffix
            bare_price = len(mentions) == 1 and not text[:mention.start()].strip() and not text[mention.end():].strip(' .!')
            named_goods = any(str(getattr(item.product, 'title', '') or '').casefold() in local_role.casefold()
                for item in items if getattr(item, 'product', None) is not None and getattr(item.product, 'title', ''))
            if not (bare_price or named_goods or goods_amount_role.search(local_role) or unit_price_re.search(local_role)):
                unclassified_amount = True
                continue
            if total_quantity <= 1 or order_total_re.search(local_role):
                goods_totals.add(amount)
            elif len(items) == 1 and unit_price_re.search(local_role):
                goods_totals.add(amount * total_quantity)
        # Different unclassified goods figures are ambiguous even when one
        # happens to equal the requested total. Require one exact goods quote.
        if goods_totals == {total} and not unclassified_amount:
            offer_index = index
    acceptance = next((row for index, row in reversed(list(enumerate(rows))) if (
        index > (offer_index if offer_index is not None else len(rows))
        and str(row.role or "").casefold() in customer_roles
        and accepts_current_commercial_offer(row.text or "", amount=total)
    )), None)
    if offer_index is None or acceptance is None:
        raise CheckoutConfigurationError("invalid_price_evidence")
    offer = rows[offer_index]
    explicit_sizes = set(re.findall(r'(?:розмір|размер|size)\s*[:=-]?\s*(XXXL|XXL|XL|XS|S|M|L)\b', offer.text, re.I))
    item_sizes = {str(item.size or '').upper() for item in items}
    if explicit_sizes and not {size.upper() for size in explicit_sizes}.issubset(item_sizes):
        raise CheckoutConfigurationError('invalid_price_evidence')
    # Do not let selected evidence IDs hide an intervening basket edit,
    # refusal, alternative price or a different manager offer.
    intervening = InstagramBotMessage.objects.filter(client=client, pk__gt=offer.pk,
        pk__lt=acceptance.pk, role__in=seller_roles | customer_roles).only('role', 'text')
    if any(row.role in seller_roles or not accepts_current_commercial_offer(row.text, amount=total) for row in intervening):
        raise CheckoutConfigurationError("invalid_price_evidence")
    # Subsequent delivery questions and recipient details do not revoke an
    # accepted price. A new commercial choice or later human price offer does.
    changed_scope = re.compile(r'(?:\b(?:ні|нет|не\s+хочу|відмов\w*|отказ\w*|замість|вместо|'
        r'інш\w*|друг\w*|змін\w*|измен\w*|замін\w*|дод\w*|добав\w*|прибер\w*|убер\w*|видал\w*|'
        r'хочу|хочемо|скид\w*|зниж\w*|промокод\w*|розмір\w*|размер\w*|size|колір|цвет|кількіст\w*|количеств\w*)\b|'
        r'\b(?:XXXL|XXL|XL|XS|S|M|L)\b|https?://|\d\s*(?:грн|uah|₴)|^\s*\d+\s*$)', re.I)
    later = InstagramBotMessage.objects.filter(client=client, pk__gt=acceptance.pk,
        role__in=seller_roles | customer_roles).only('role', 'text')
    if any(changed_scope.search(row.text or '')
            or row.role in seller_roles and amount_re.search(row.text or '') for row in later):
        raise CheckoutConfigurationError('invalid_price_evidence')


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
    source_cart_binding=None,
    verified_assisted_decision=None,
    verified_assisted_selection=None,
):
    from product_catalog.services import effective_cart_unit_price, variant_allows_purchase
    from management.services.ig_inventory_availability import inventory_source_for_variant
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
    source_lines = None
    if verified_assisted_decision is not None:
        from management.services.ig_assisted_commerce import VerifiedAssistedDecision
        cap = verified_assisted_decision
        if type(cap) is not VerifiedAssistedDecision or cap.client_id != client.pk:
            raise CheckoutConfigurationError('assisted_decision_requires_native_producer')
        catalog_lines = [line for line in cap.lines if line.item_kind == 'catalog_apparel']
        if len(catalog_lines) != len(item_specs) or len(cap.ready_lines) != len(cap.lines):
            raise CheckoutConfigurationError('assisted_full_ready_required')
        for approved, item in zip(catalog_lines, item_specs, strict=True):
            if item != assisted_catalog_spec(approved):
                raise CheckoutConfigurationError('assisted_catalog_configuration_changed')
        source_lines = [{'recipient_id': line.recipient_id} for line in catalog_lines]
    if verified_assisted_selection is not None:
        from management.services.ig_assisted_scopes import VerifiedAssistedSelection
        selected = verified_assisted_selection
        if (verified_assisted_decision is not None or type(selected) is not VerifiedAssistedSelection
                or selected.client_id != client.pk):
            raise CheckoutConfigurationError('assisted_selection_requires_native_producer')
        catalog_lines = [line for line in selected.lines if line.item_kind == 'catalog_apparel']
        if (len(catalog_lines) != len(item_specs) or any(line.ready_capacity != line.quantity for line in selected.lines)):
            raise CheckoutConfigurationError('assisted_selected_ready_required')
        for approved, item in zip(catalog_lines, item_specs, strict=True):
            if item != assisted_catalog_spec(approved):
                raise CheckoutConfigurationError('assisted_catalog_configuration_changed')
        source_lines = [{'recipient_id': line.recipient_id} for line in catalog_lines]
    if source_cart_binding is not None:
        from management.services.ig_revision_cart_binding import validate_quote_source_positions
        source = validate_quote_source_positions(binding=source_cart_binding,
            client_id=client.pk, item_specs=item_specs)
        if not source.ok:
            raise CheckoutConfigurationError("source_cart_binding_invalid", reason=source.reason)
        source_lines = source.binding["lines"]

    evidence = evidence if isinstance(evidence, dict) else {}
    evidence_ids = _normalize_message_ids(evidence.get("message_ids"))
    normalized = []
    identities = {}
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

        product = Product.objects.select_related("category", "catalog", "size_grid").filter(pk=product_id).first()
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
        from management.services.ig_catalog_pricing import prepare_pricing_context
        prepare_pricing_context([product], color_variants)
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
                    **({"ignore_variant_stock": True} if inventory_source_for_variant(row) == "warehouse" else {}),
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
                    **({"ignore_variant_stock": True} if variant is not None and inventory_source_for_variant(variant) == "warehouse" else {}),
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
            **({"ignore_variant_stock": True} if variant is not None and inventory_source_for_variant(variant) == "warehouse" else {}),
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
        previous = identities.get(identity, [])
        if previous:
            if source_lines is None:
                raise CheckoutConfigurationError("duplicate_items", item_index=index)
            recipient = source_lines[index]["recipient_id"]
            if any(source_lines[position]["recipient_id"] == recipient for position in previous):
                raise CheckoutConfigurationError("duplicate_recipient_items", item_index=index,
                    reason="source_cart_quantity_clarification_required")
        identities.setdefault(identity, []).append(index)

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

    from storefront.services.brigade_commerce import calculate_brigade_cart_pricing
    pricing_cart = {
        str(index): {**item.digest_payload(), "qty": item.quantity, "fit_option_code": item.fit_code}
        for index, item in enumerate(normalized)
    }
    brigade_pricing = calculate_brigade_cart_pricing(
        pricing_cart,
        {item.product.pk: item.product for item in normalized},
        {item.color_variant.pk: item.color_variant for item in normalized if item.color_variant},
    )
    normalized = [
        replace(item, catalog_unit_price=brigade_pricing.lines[str(index)].unit_price,
                catalog_line_total=brigade_pricing.lines[str(index)].line_total,
                price_parts=tuple(brigade_pricing.lines[str(index)].frozen_price_parts())
                    if brigade_pricing.lines[str(index)].bundle_allocations else (),
                bundle_pricing=brigade_pricing.lines[str(index)].bundle_metadata()
                    if brigade_pricing.lines[str(index)].bundle_allocations else {})
        for index, item in enumerate(normalized)
    ]
    # Distinct recipients retain distinct quote lines, but consume the same
    # tracked variant stock. Check their aggregate using the existing stock
    # semantics (zero is untracked/made-to-order, never invented scarcity).
    variant_quantities = {}
    for index, item in enumerate(normalized):
        if item.color_variant is not None:
            variant_quantities.setdefault(item.color_variant.pk, [item.color_variant, 0, index])[1] += item.quantity
    for variant, quantity, index in variant_quantities.values():
        if tracked_stock_shortfall(variant, quantity):
            raise CheckoutConfigurationError("insufficient_stock", item_index=index,
                reason="tracked_stock_shortfall")
    catalog_total = sum((item.catalog_line_total for item in normalized), Decimal("0.00"))
    if catalog_total <= 0 or catalog_total > MAX_CHECKOUT_VALUE:
        raise CheckoutConfigurationError("invalid_catalog_total")
    quoted_total = catalog_total
    if negotiated_total is not None:
        quoted_total = _money(negotiated_total, code="invalid_negotiated_total")
        if quoted_total > catalog_total:
            raise CheckoutConfigurationError("invalid_negotiated_total")
        if catalog_total - quoted_total > brigade_pricing.promo_eligible_subtotal:
            raise CheckoutConfigurationError("brigade_discount_unavailable" if brigade_pricing.full_payment_only else "bundle_discount_unavailable")
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
    if brigade_pricing.full_payment_only and normalized_pay_type != "online_full":
        raise CheckoutConfigurationError("brigade_full_payment_required")
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


def assisted_catalog_spec(line):
    return {'product_id': line.product_id, 'color_variant_id': line.color_variant_id,
        'qty': line.quantity, 'size': line.size, 'fit_option_code': line.fit_code,
        'option_values': line.as_dict()['option_values']}


def assisted_item_snapshot(item):
    """Explicit custom facts/proof; no generic-option authority channel."""
    return {'product_id': None, 'color_variant_id': None, 'is_custom': True,
        'title': item.product_title, 'color_name_custom': item.color_label,
        'size': item.size, 'fit_option_code': item.fit_code, 'fit_option_label': item.fit_label,
        'option_values': item.option_values, 'option_labels': item.option_labels,
        'qty': item.quantity, 'unit_price': str(item.catalog_unit_price),
        'line_total': str(item.catalog_line_total), 'currency': 'UAH',
        'photo_asset_id': item.photo_asset_id, 'photo_digest': item.photo_digest,
        'assisted_line': dict(item.assisted_line)}


def build_assisted_quote(*, client, verified_decision=None, verified_selection=None):
    """Re-read native approval, price catalogue subset ordinarily, quote all READY."""
    if verified_selection is not None:
        if verified_decision is not None:
            raise CheckoutConfigurationError('assisted_authority_ambiguous')
        from management.services.ig_assisted_scopes import revalidate_verified_assisted_selection
        selected = revalidate_verified_assisted_selection(verified_selection, client=client)
        if selected.proof_scope != 'unconsumed':
            raise CheckoutConfigurationError('assisted_existing_execution_requires_native_verifier')
        return _build_assisted_quote(client=client, cap=selected, selected=True)
    from management.services.ig_assisted_commerce import revalidate_verified_assisted_decision
    cap = revalidate_verified_assisted_decision(verified_decision, client=client)
    if cap.proof_scope != 'unconsumed':
        raise CheckoutConfigurationError('assisted_existing_execution_requires_native_verifier')
    return _build_assisted_quote(client=client, cap=cap)


def observe_assisted_quote(*, client, verified_decision=None, verified_selection=None):
    """Read one quote from the enclosing read capture's already verified cap.

    No approval/source/choice graph is re-read here. Callers own the same
    request-local capture and its final fences. This observation authorizes no
    proposal, reservation or provider effect; those producers always revalidate.
    Catalogue rows still pass the ordinary live configuration/pricing resolver.
    """
    from management.services import ig_assisted_commerce as commerce
    from management.services import ig_assisted_scopes as scopes
    if (verified_decision is None) == (verified_selection is None):
        raise CheckoutConfigurationError('assisted_authority_ambiguous')
    selected = verified_selection is not None
    cap = verified_selection if selected else verified_decision
    expected, issuer = (scopes.VerifiedAssistedSelection, scopes._ISSUER) if selected else (commerce.VerifiedAssistedDecision, commerce._ISSUER)
    if (type(cap) is not expected or cap._issuer is not issuer or cap.historical
        or cap.proof_scope not in {'unconsumed', 'execution'} or not client or cap.client_id != client.pk):
        raise CheckoutConfigurationError('assisted_observation_requires_native_capture')
    return _build_assisted_quote(client=client, cap=cap, selected=selected)


def rebuild_assisted_quote_from_execution(*, client, verified_execution):
    """Readonly existing quote rebuild after the native Execution owner rechecks."""
    from management.services.ig_assisted_checkout import revalidate_verified_assisted_execution
    cap = revalidate_verified_assisted_execution(verified_execution, client=client)
    if cap.proof_scope != 'execution':
        raise CheckoutConfigurationError('assisted_execution_quote_scope_invalid')
    from management.services.ig_assisted_scopes import VerifiedAssistedSelection
    return _build_assisted_quote(client=client, cap=cap, selected=type(cap) is VerifiedAssistedSelection)


def _build_assisted_quote(*, client, cap, selected=False):
    from management.services.ig_revision_cart_binding import assisted_quote_evidence
    if (not cap.lines or len(cap.lines) > MAX_CHECKOUT_ITEMS
            or (any(line.ready_capacity != line.quantity for line in cap.lines) if selected else len(cap.ready_lines) != len(cap.lines))):
        raise CheckoutConfigurationError('assisted_full_ready_required')
    if cap.currency != 'UAH' or sum(line.quantity for line in cap.lines) > MAX_CHECKOUT_QUANTITY:
        raise CheckoutConfigurationError('assisted_cart_bounds')
    catalog_lines = [line for line in cap.lines if line.item_kind == 'catalog_apparel']
    catalog_quote = None
    if catalog_lines:
        catalog_quote = validate_checkout_items(client=client,
            item_specs=[assisted_catalog_spec(line) for line in catalog_lines],
            **({"verified_assisted_selection": cap} if selected else {"verified_assisted_decision": cap}))
    catalog_rows = iter(catalog_quote.items if catalog_quote else ())
    normalized = []
    for line in cap.lines:
        identity = {'line_id': line.line_id, 'recipient_id': line.recipient_id,
            'original_index': line.original_index, 'decision_id': str(cap.decision_id),
            'decision_digest': cap.decision_digest, 'line_digest': line.line_digest}
        if line.item_kind == 'catalog_apparel':
            item = next(catalog_rows)
            if (item.product.pk != line.product_id
                or (item.color_variant.pk if item.color_variant else None) != line.color_variant_id
                or item.product_title != line.title or item.color_label != line.color
                or item.size != line.size or item.fit_code != line.fit_code
                or item.option_values != line.as_dict()['option_values']):
                raise CheckoutConfigurationError('assisted_catalog_facts_changed')
            if (item.quantity != line.quantity or item.catalog_line_total != line.ready_total
                or item.catalog_unit_price != line.unit_price):
                raise CheckoutConfigurationError('assisted_catalog_price_changed')
            normalized.append(replace(item, assisted_line=identity))
            continue
        if line.item_kind != 'custom_apparel' or line.product_id is not None or line.color_variant_id is not None:
            raise CheckoutConfigurationError('assisted_custom_identity_invalid')
        if (not isinstance(line.unit_price, Decimal) or not line.unit_price.is_finite()
            or not Decimal('0') < line.unit_price <= MAX_CHECKOUT_VALUE
            or line.unit_price != line.unit_price.quantize(Decimal('.01'))):
            raise CheckoutConfigurationError('assisted_custom_price_invalid')
        item = ValidatedCheckoutItem(product=None, color_variant=None, quantity=line.quantity,
            size=line.size, fit_code=line.fit_code, fit_label=line.fit_code,
            option_values={}, option_labels={}, catalog_unit_price=line.unit_price,
            catalog_line_total=line.ready_total, product_title=line.title, sku='', image_url='',
            color_code='', color_label=line.color, evidence_message_ids=(), assisted_line=identity,
            photo_asset_id=str(line.image_id) if line.image_id else None, photo_digest=line.image_digest)
        item.digest_payload()  # Complete bounded model-compatible custom facts before effects.
        normalized.append(item)
    total = sum((item.catalog_line_total for item in normalized), Decimal('0.00'))
    if not total.is_finite() or not Decimal('0') < total <= MAX_CHECKOUT_VALUE or total != cap.approved_total:
        raise CheckoutConfigurationError('assisted_approved_total_changed')
    if selected:
        from management.services.ig_assisted_scopes import selection_evidence
        evidence = selection_evidence(cap)
    else:
        evidence = assisted_quote_evidence(cap)
    digest = hashlib.sha256(json.dumps({'schema': 'ig-assisted-selected-quote.v1' if selected else 'ig-assisted-quote.v1',
        'items': [item.digest_payload() for item in normalized], 'assisted_commerce': evidence,
        'quoted_total': str(total), 'pay_type': 'online_full', 'allow_promo': False},
        sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    return ValidatedQuote(tuple(normalized), total, Decimal('0.00'), total, total,
        'online_full', (), digest, evidence)


def _deal_item_snapshot(item):
    if item.product is None:
        return {**assisted_item_snapshot(item), 'price_source': 'manager_custom',
            'price_evidence_message_ids': []}
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
        **({"price_parts": list(item.price_parts), "bundle_pricing": item.bundle_pricing} if item.price_parts else {}),
        "price_source": (
            "catalog_with_order_discount" if item.evidence_message_ids else "catalog"
        ),
        "price_evidence_message_ids": list(item.evidence_message_ids),
        **({'assisted_line': dict(item.assisted_line)} if item.assisted_line else {}),
    }


def _revision_snapshot(quote, *, source_cart_binding=None, source_cart_capture=None):
    snapshot = {
        "items": [_deal_item_snapshot(item) for item in quote.items],
        "catalog_total": str(quote.catalog_total),
        "negotiated_discount": str(quote.negotiated_discount),
        "quoted_total": str(quote.quoted_total),
        "requested_payment_amount": str(quote.requested_payment_amount),
        "pay_type": quote.pay_type,
        "digest": quote.digest,
    }
    if source_cart_binding is not None:
        from copy import deepcopy
        snapshot["source_cart_binding"] = deepcopy(source_cart_binding)
        snapshot["source_cart_capture"] = deepcopy(source_cart_capture)
    if quote.assisted_commerce:
        snapshot['assisted_commerce'] = dict(quote.assisted_commerce)
    return snapshot


def _same_revision_source_cart(proposal, binding, capture):
    """Money equality cannot replay a different source/recipient/head artifact."""
    revision = proposal.revisions.filter(revision=proposal.revision).first()
    snapshot = revision.snapshot if revision is not None else {}
    if not isinstance(snapshot, dict):
        return False
    if binding is None:
        return "source_cart_binding" not in snapshot and "source_cart_capture" not in snapshot
    from management.services.ig_revision_cart_binding import ASSISTED_SCHEMA, validate_assisted_cart_binding
    if isinstance(binding, dict) and binding.get('schema') == ASSISTED_SCHEMA:
        checked = validate_assisted_cart_binding(binding, episode_id=proposal.commercial_episode_id)
        return bool(checked.ok and snapshot.get('source_cart_binding') == checked.binding
            and snapshot.get('source_cart_capture') == capture)
    from management.services.ig_revision_cart_binding import same_checkout_source_capture
    if not same_checkout_source_capture(snapshot.get("source_cart_capture"), capture):
        return False
    # Rebuild the original frozen binding against today's canonical source and
    # strict catalog readiness. Only its approved owner hash may have changed;
    # original capture, proofs, source hash and quote map stay intact.
    from management.services.ig_revision_checkout import checkout_authority_control
    original, reasons = checkout_authority_control(proposal.client, {
        "source_cart_capture": snapshot["source_cart_capture"],
        "source_cart_binding": snapshot.get("source_cart_binding"),
    })
    return (not reasons and original.get("source_cart_binding") == snapshot.get("source_cart_binding")
            and original["source_cart_binding"]["quote_line_map"] == binding.get("quote_line_map"))


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
                "manager_custom" if item.product is None else "catalog_with_order_discount"
                if quote.negotiated_discount > 0
                else "catalog"
            ),
            evidence_message_ids=list(item.evidence_message_ids),
            position=position,
        ))
    IgCheckoutProposalItem.objects.bulk_create(rows)


def assert_untouched_legacy_assisted_proposal(*, client, proposal, deal, episode_id):
    """Read-only admission shared by native approval and its locked upgrade.

    Callers hold the canonical client/deal/proposal transaction locks. Historical
    invoice generations and detached attempts count even when fast pointers are
    empty; an issued payment graph must retain its original policy and terms.
    """
    from management.models import IgCheckoutProposal, IgDeal
    from orders.models import PaymentAttempt

    if (
        proposal.client_id != client.pk or deal.client_id != client.pk
        or proposal.deal_id != deal.pk or deal.active_checkout_proposal_id != proposal.pk
        or episode_id is None or client.current_commercial_episode_id != episode_id
        or proposal.commercial_episode_id != episode_id
        or proposal.assisted_checkout_v2
        or proposal.payment_policy != IgCheckoutProposal.PaymentPolicy.LEGACY
        or proposal.status not in {IgCheckoutProposal.Status.READY, IgCheckoutProposal.Status.VIEWED}
        or proposal.is_expired or proposal.details_locked_at is not None
        or proposal.payment_attempt_id or proposal.current_invoice_generation_id
        or proposal.winner_invoice_generation_id or proposal.paid_at is not None
        or proposal.invoice_cancelled_at is not None or proposal.provider_cancellation_event_id
        or proposal.superseded_by_id or deal.invoice_id or deal.invoice_url
        or deal.order_id or deal.paid_at is not None or deal.paid_amount > 0
        or deal.status in {IgDeal.Status.PAID, IgDeal.Status.ORDER_CREATED}
        or deal.payment_truth in {IgDeal.PaymentTruth.CONFIRMED,
            IgDeal.PaymentTruth.PARTIALLY_REFUNDED, IgDeal.PaymentTruth.REFUNDED,
            IgDeal.PaymentTruth.REVERSED}
        or proposal.invoice_generations.exists()
        or PaymentAttempt.objects.filter(cart_snapshot__proposal_id=str(proposal.public_id)).exists()
    ):
        raise CheckoutConfigurationError('assisted_legacy_proposal_not_untouched')


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
    source_cart_binding=None,
    source_cart_capture=None,
    checkout_owner_scope=None,
    verified_assisted_decision=None,
    verified_selection=None,
    verified_checkout_amendment=None,
    _retired_amendment_proposal=None,
):
    from management.models import (
        IgCheckoutProposal,
        IgCheckoutRevision,
        IgClient,
        IgCommercialEpisode,
        IgDeal,
    )

    locked_client = IgClient.objects.select_for_update().get(pk=client.pk)
    if verified_checkout_amendment is not None and (verified_assisted_decision is not None or verified_selection is not None):
        raise CheckoutConfigurationError("checkout_amendment_authority_ambiguous")
    if _retired_amendment_proposal is not None:
        retired = _retired_amendment_proposal
        if (type(retired) is not _RetiredAmendmentProposal or retired.issuer is not _AMENDMENT_SUCCESSOR_ISSUER
                or retired.client_id != locked_client.pk or deal is None or retired.deal_id != deal.pk
                or (verified_assisted_decision is None and verified_selection is None)):
            raise CheckoutConfigurationError("checkout_successor_requires_native_factory")
    locale_code = str(locale or getattr(locked_client, "language", "") or "uk").lower().replace("_", "-").split("-", 1)[0]
    if locale_code not in {"uk", "ru", "en"}:
        locale_code = "uk"
    from orders.checkout_series import assisted_checkout_v2_new_proposal_enabled

    new_v2_canary = assisted_checkout_v2_new_proposal_enabled(locked_client.pk)
    assisted = verified_assisted_decision is not None or verified_selection is not None
    selected = verified_selection is not None
    if selected:
        if verified_assisted_decision is not None:
            raise CheckoutConfigurationError('assisted_authority_ambiguous')
        from management.services.ig_assisted_scopes import revalidate_verified_assisted_selection
        from management.services.ig_revision_cart_binding import build_selected_cart_binding, validate_selected_cart_binding
        verified_selection = revalidate_verified_assisted_selection(verified_selection, client=locked_client)
        if verified_selection.proof_scope != 'unconsumed':
            raise CheckoutConfigurationError('assisted_existing_execution_cannot_create_proposal')
    elif assisted:
        from management.services.ig_assisted_commerce import revalidate_verified_assisted_decision
        from management.services.ig_revision_cart_binding import (
            build_assisted_cart_binding, validate_assisted_cart_binding,
        )
        verified_assisted_decision = revalidate_verified_assisted_decision(
            verified_assisted_decision, client=locked_client)
        if verified_assisted_decision.proof_scope != 'unconsumed':
            raise CheckoutConfigurationError('assisted_existing_execution_cannot_create_proposal')
    custom_print_requested = bool(
        any(
            isinstance(row, dict)
            and row.get("custom_print_lead_id") not in (None, "", 0, "0")
            for row in item_specs or ()
        )
    )
    quote = build_assisted_quote(client=locked_client,
        **({'verified_selection': verified_selection} if selected else {'verified_decision': verified_assisted_decision})) if assisted else validate_checkout_items(
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
        source_cart_binding=source_cart_binding,
    )
    bound_cart = source_cart_binding is not None or source_cart_capture is not None or checkout_owner_scope is not None
    if assisted:
        binding_builder = build_selected_cart_binding if selected else build_assisted_cart_binding
        expected_binding = binding_builder(
            **({'verified_selection': verified_selection} if selected else {'verified_decision': verified_assisted_decision}),
            quoted_items=[_deal_item_snapshot(item) for item in quote.items],
            bound_episode_id=(source_cart_binding or {}).get('bound_episode_id'))
        if source_cart_binding is not None and source_cart_binding != expected_binding:
            raise CheckoutConfigurationError('assisted_cart_binding_changed')
        source_cart_binding = expected_binding
        source_cart_capture = verified_selection.current_capture if selected else verified_assisted_decision.full_capture
        checked = (validate_selected_cart_binding if selected else validate_assisted_cart_binding)(source_cart_binding)
        if not checked.ok or (source_cart_capture['scope']['episode_id'] is not None
                and source_cart_capture['scope']['episode_id'] != locked_client.current_commercial_episode_id):
            raise CheckoutConfigurationError('assisted_cart_owner_changed')
        if allow_promo or negotiated_total is not None or requested_payment_amount is not None:
            raise CheckoutConfigurationError('assisted_terms_override_forbidden')
        bound_cart = True
    elif verified_checkout_amendment is not None:
        from management.services.ig_checkout_cart_amendments import verify_prepared_amendment
        if assisted:
            raise CheckoutConfigurationError("checkout_amendment_authority_ambiguous")
        receipt = verify_prepared_amendment(verified_checkout_amendment, client=locked_client,
            item_specs=item_specs, binding=source_cart_binding, capture=source_cart_capture,
            pay_type=pay_type, allow_promo=allow_promo)
        if deal is None or receipt.proposal.deal_id != deal.pk:
            raise CheckoutConfigurationError("checkout_amendment_owner_changed")
    elif bound_cart:
        # Reconstruct all selectors from the canonical capture while the client
        # is locked. This is not an optional caller-authenticated cart payload.
        from management.services.ig_revision_checkout import checkout_authority_control, _quote_items, lock_checkout_cart_sources
        if not lock_checkout_cart_sources(locked_client, source_cart_capture):
            raise CheckoutConfigurationError("checkout_cart_source_scope_invalid")
        candidate, reasons = checkout_authority_control(locked_client, {
            "source_cart_binding": source_cart_binding, "source_cart_capture": source_cart_capture,
            "checkout_owner_scope": checkout_owner_scope,
        })
        if reasons or candidate.get("items") != _quote_items(quote):
            raise CheckoutConfigurationError(reasons[0] if reasons else "checkout_cart_configuration_changed")
    # A matching price/configuration digest cannot identify a purchase cycle.
    # Locate only the client's exact current episode; NULL legacy scope never
    # borrows the latest historical deal (which may already own paid money).
    current_episode_id = locked_client.current_commercial_episode_id
    null_open_episode_id = None
    if current_episode_id is None:
        open_episode = IgCommercialEpisode.objects.filter(
            client_id=locked_client.pk, open_slot=1,
        ).values("pk", "deal_id", "state", "intended_order_id").first()
        if open_episode is not None:
            if (open_episode["deal_id"] is not None or open_episode["intended_order_id"] is not None
                    or open_episode["state"] != IgCommercialEpisode.State.ACTIVE):
                # Missing current scope is not a request to supersede another
                # open, already-bound purchase. Its source owner must resolve it.
                raise CheckoutConfigurationError("checkout_episode_scope_unknown")
            null_open_episode_id = open_episode["pk"]

    def current_episode_scope():
        if current_episode_id is None:
            return None
        episode = IgCommercialEpisode.objects.filter(
            pk=current_episode_id, client_id=locked_client.pk,
        ).values("pk", "deal_id", "state", "open_slot", "intended_order_id").first()
        if episode is None:
            raise CheckoutConfigurationError("checkout_episode_scope_changed")
        if episode["intended_order_id"] is not None or episode["state"] in {
            IgCommercialEpisode.State.ORDER_CREATED, IgCommercialEpisode.State.FULFILLED,
        }:
            raise CheckoutConfigurationError("checkout_episode_converted")
        if episode["state"] != IgCommercialEpisode.State.ACTIVE or episode["open_slot"] != 1:
            raise CheckoutConfigurationError("checkout_episode_stale")
        return episode

    current_episode = current_episode_scope()
    if deal is not None:
        deal = (
            IgDeal.objects.select_for_update()
            .filter(pk=deal.pk, client=locked_client)
            .first()
        )
        if deal is None:
            raise CheckoutConfigurationError("invalid_deal")
    elif current_episode is not None and current_episode["deal_id"] is not None:
        deal = (
            IgDeal.objects.select_for_update()
            .filter(pk=current_episode["deal_id"], client=locked_client)
            .first()
        )
        if deal is None:
            raise CheckoutConfigurationError("checkout_episode_scope_changed")
    # Preserve the existing Deal -> Proposal -> Attempt lock prefix. Episode
    # reads add no earlier episode lock; re-read after a deal-lock wait so a
    # concurrently completed conversion cannot pass the same-digest shortcut.
    if deal is not None:
        current_episode = current_episode_scope()
        deal_episode_id = IgCommercialEpisode.objects.filter(deal_id=deal.pk).values_list("pk", flat=True).first()
        if (current_episode is None and deal_episode_id is not None) or (
            current_episode is not None and (
                current_episode["deal_id"] not in {None, deal.pk}
                or deal_episode_id not in {None, current_episode_id}
            )
        ):
            raise CheckoutConfigurationError("checkout_episode_scope_changed")
        if (deal.order_id or deal.paid_at is not None or deal.paid_amount > 0
                or deal.status in {IgDeal.Status.PAID, IgDeal.Status.ORDER_CREATED}
                or deal.payment_truth in {
                    IgDeal.PaymentTruth.CONFIRMED, IgDeal.PaymentTruth.PARTIALLY_REFUNDED,
                    IgDeal.PaymentTruth.REFUNDED, IgDeal.PaymentTruth.REVERSED,
                }):
            raise CheckoutConfigurationError("checkout_episode_converted")
    proposal_id = deal.active_checkout_proposal_id if deal is not None else None
    detached_terminal_proposal = False
    if deal is not None and proposal_id is None:
        previous_proposal = IgCheckoutProposal.objects.filter(
            deal_id=deal.pk,
        ).order_by("-pk").values("pk", "status", "commercial_episode_id", "client_id").first()
        if previous_proposal is not None:
            retired_match = (_retired_amendment_proposal is not None
                and previous_proposal["pk"] == _retired_amendment_proposal.successor_id
                and previous_proposal["status"] == IgCheckoutProposal.Status.READY
                and previous_proposal["client_id"] == locked_client.pk
                and previous_proposal["commercial_episode_id"] == current_episode_id)
            if retired_match:
                # Only the native wrapper can mint this per-call witness after
                # verifying fresh approval and retiring this exact pre-bank P.
                previous_proposal = None
        if previous_proposal is not None:
            if (previous_proposal["client_id"] != locked_client.pk
                    or previous_proposal["commercial_episode_id"] != current_episode_id
                    or previous_proposal["status"] not in {
                        IgCheckoutProposal.Status.EXPIRED, IgCheckoutProposal.Status.CANCELLED,
                    }):
                raise CheckoutConfigurationError("checkout_episode_scope_changed")
            # Local terminalization intentionally clears the fast pointer.
            # Its exact current-deal receipt may still authorize the existing
            # fresh-deal repair below; a historical proposal is never a fallback.
            proposal_id = previous_proposal["pk"]
            detached_terminal_proposal = True
    proposal = None
    if proposal_id:
        proposal = IgCheckoutProposal.objects.select_for_update().filter(pk=proposal_id).first()
        if proposal is None:
            raise CheckoutConfigurationError("checkout_episode_scope_changed")
        current_episode_scope()
        if (proposal.client_id != locked_client.pk or proposal.deal_id != deal.pk
                or proposal.commercial_episode_id != current_episode_id):
            raise CheckoutConfigurationError("checkout_episode_scope_changed")
        if (proposal.status == IgCheckoutProposal.Status.PAID
                or proposal.paid_at is not None or proposal.winner_invoice_generation_id is not None):
            raise CheckoutConfigurationError("checkout_episode_converted")
        if not bound_cart:
            original_revision = proposal.revisions.filter(revision=proposal.revision).first()
            if original_revision is not None and isinstance(original_revision.snapshot, dict) and "source_cart_binding" in original_revision.snapshot:
                # An unbound legacy retry/edit cannot remove proven line and
                # recipient ownership from an already bound current offer.
                raise CheckoutConfigurationError("checkout_cart_binding_missing")
    activate_v2 = bool(assisted or (
        proposal.assisted_checkout_v2 if proposal is not None else new_v2_canary
    ))
    upgrade_legacy_assisted = bool(assisted and proposal is not None and not proposal.assisted_checkout_v2)
    if upgrade_legacy_assisted:
        assert_untouched_legacy_assisted_proposal(client=locked_client, proposal=proposal,
            deal=deal, episode_id=current_episode_id)
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
            source_cart_binding=source_cart_binding,
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
            source_cart_binding=source_cart_binding,
        )
    payment_policy = None
    if activate_v2:
        from management.services.ig_checkout_policy import resolve_payment_policy

        payment_policy = resolve_payment_policy(
            client=locked_client,
            evidence_message_ids=quote.evidence_message_ids,
            custom_print_full_only=(
                assisted
                or
                custom_print_requested
                or bool(getattr(proposal, "custom_print_full_only", False))
            ),
        )
    if activate_v2 and payment_policy is not None:
        from storefront.services.brigade_commerce import products_require_full_payment
        if products_require_full_payment([item.product for item in quote.items if item.product is not None]):
            payment_policy = replace(payment_policy, policy=IgCheckoutProposal.PaymentPolicy.FULL_ONLY)
    terminal_fresh_deal = False
    if proposal is not None:
        local_terminal = {}
        if proposal.payment_attempt_id:
            from orders.models import PaymentAttempt

            locked_attempt = PaymentAttempt.objects.select_for_update().get(
                pk=proposal.payment_attempt_id
            )
            if locked_attempt.order_id is not None or locked_attempt.status in {
                PaymentAttempt.Status.PAID, PaymentAttempt.Status.PREPAID, PaymentAttempt.Status.CONVERTED,
            }:
                raise CheckoutConfigurationError("checkout_episode_converted")
            local_terminal = dict(
                (locked_attempt.event_state or {}).get("local_terminalization") or {}
            )
        if detached_terminal_proposal and not local_terminal:
            raise CheckoutConfigurationError("checkout_episode_scope_changed")
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
            terminal_fresh_deal = True
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
        elif (not upgrade_legacy_assisted and proposal.items_digest == quote.digest and proposal.allow_promo == bool(allow_promo)
              and _same_revision_source_cart(proposal, source_cart_binding, source_cart_capture)):
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
    if (episode.client_id != locked_client.pk or episode.deal_id != deal.pk
            or episode.state != IgCommercialEpisode.State.ACTIVE or episode.open_slot != 1
            or episode.intended_order_id is not None
            or (null_open_episode_id is not None and episode.pk != null_open_episode_id)
            or (current_episode_id is not None and not terminal_fresh_deal and episode.pk != current_episode_id)):
        raise CheckoutConfigurationError("checkout_episode_scope_changed")
    if locked_client.current_commercial_episode_id != episode.pk:
        # Bind the exact materialized owner, including NULL legacy pointers
        # and the existing proven local-terminal fresh-deal repair.
        locked_client.current_commercial_episode = episode
        locked_client.save(update_fields=["current_commercial_episode", "updated_at"])
    if assisted:
        from management.services.ig_revision_cart_binding import rebind_assisted_cart_episode, rebind_selected_cart_episode
        source_cart_binding = (rebind_selected_cart_episode if selected else rebind_assisted_cart_episode)(source_cart_binding, episode.pk)
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
        values = {"commercial_episode": episode, "catalog_total": quote.catalog_total,
            "negotiated_discount": quote.negotiated_discount, "quoted_total": quote.quoted_total,
            "requested_payment_amount": quote.requested_payment_amount, "pay_type": quote.pay_type,
            "allow_promo": bool(allow_promo), "items_digest": quote.digest, "locale": locale_code,
            **({**proposal_values, "expires_at": min(proposal_values.get("expires_at", _retired_amendment_proposal.expires_at),
                _retired_amendment_proposal.expires_at)} if _retired_amendment_proposal is not None else proposal_values)}
        if _retired_amendment_proposal is None:
            proposal = IgCheckoutProposal.objects.create_current(deal=deal, **values)
        else:
            proposal = IgCheckoutProposal.objects.select_for_update().get(pk=_retired_amendment_proposal.successor_id,
                client=locked_client, deal=deal, commercial_episode=episode, status=IgCheckoutProposal.Status.READY)
            origin = IgCheckoutProposal.objects.select_for_update().get(pk=_retired_amendment_proposal.proposal_id,
                client=locked_client, deal=deal, commercial_episode=episode)
            if (proposal.items.exists() or proposal.revisions.exists() or proposal.payment_attempt_id
                    or proposal.invoice_generations.exists() or origin.status != _retired_amendment_proposal.origin_status
                    or (origin.status == IgCheckoutProposal.Status.SUPERSEDED and origin.superseded_by_id != proposal.pk)
                    or proposal.items_digest != quote.digest or proposal.catalog_total != quote.catalog_total
                    or proposal.quoted_total != quote.quoted_total):
                raise CheckoutConfigurationError("checkout_reserved_successor_changed")
            for field, value in values.items():
                setattr(proposal, field, value)
            proposal.save(update_fields=[*values, "updated_at"])
            deal.active_checkout_proposal = proposal
            deal.save(update_fields=["active_checkout_proposal", "updated_at"])
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
            if upgrade_legacy_assisted:
                from management.services.ig_checkout_policy import PROPOSAL_TTL_SECONDS

                proposal.assisted_checkout_v2 = True
                proposal.expires_at = timezone.now() + timedelta(seconds=PROPOSAL_TTL_SECONDS)
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
            *(["assisted_checkout_v2", "expires_at"] if upgrade_legacy_assisted else []),
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
        revision_source = (IgCheckoutRevision.Source.CHECKOUT_HOLDER
            if verified_checkout_amendment is not None else IgCheckoutRevision.Source.BOT_UPDATE)

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
        snapshot=_revision_snapshot(quote, source_cart_binding=source_cart_binding, source_cart_capture=source_cart_capture),
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
        stage_event = IgClientStageEvent.objects.create(
            client=locked_client,
            from_stage=stage_before or "",
            to_stage=IgClient.Stage.CHECKOUT,
            reason="checkout_proposal_created",
        )
        from management.services.ig_commercial_episodes import append_client_stage_transition_event
        append_client_stage_transition_event(
            locked_client, stage_event_id=stage_event.pk, from_stage=stage_before or "",
            to_stage=IgClient.Stage.CHECKOUT, reason="checkout_proposal_created",
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


@transaction.atomic
def create_approved_amendment_successor(*, client, verified_amendment,
        verified_assisted_decision=None, verified_selection=None, locale=None):
    """Retire one untouched approval only after a fresh native decision exists.

    Original proposal items/revisions and D/E stay immutable. Current Deal and
    episode projections move through the existing proposal/quote engine.
    """
    from management.models import IgClient, IgDeal, IgCheckoutProposal, IgCheckoutCartAmendment
    from management.services.ig_checkout_cart_amendments import (
        VerifiedPendingCheckoutAmendment, VerifiedCancelledCheckoutAmendment,
        verify_pending_amendment, verify_cancelled_amendment, _ISSUER,
    )
    if (type(verified_amendment) not in {VerifiedPendingCheckoutAmendment, VerifiedCancelledCheckoutAmendment}
            or verified_amendment._issuer is not _ISSUER or verified_amendment.client_id != client.pk
            or (verified_assisted_decision is None) == (verified_selection is None)):
        raise CheckoutConfigurationError("checkout_successor_requires_native_approval")
    locked_client = IgClient.objects.select_for_update().get(pk=client.pk)
    cancelled = type(verified_amendment) is VerifiedCancelledCheckoutAmendment
    checked = (verify_cancelled_amendment if cancelled else verify_pending_amendment)(verified_amendment.amendment_id)
    if checked.get("ready") is not True or checked["amendment"].source_hmac != verified_amendment.source_hmac:
        raise CheckoutConfigurationError(checked.get("reason") or "checkout_amendment_changed")
    receipt = IgCheckoutCartAmendment.objects.select_for_update().get(pk=verified_amendment.amendment_id)
    # Revalidate exact new manager terms/configuration before retiring anything.
    quote = build_assisted_quote(client=locked_client, **({"verified_selection": verified_selection}
        if verified_selection is not None else {"verified_decision": verified_assisted_decision}))
    cap = verified_selection if verified_selection is not None else verified_assisted_decision
    request_id = getattr(cap, "request_id", None)
    from management.services.ig_checkout_amendment_successors import verify_amendment_request
    from management.models import IgAssistedCommerceRequest
    request = IgAssistedCommerceRequest.objects.get(pk=request_id, client=locked_client)
    native_request = verify_amendment_request(request, client=locked_client, now=timezone.now())
    if not native_request:
        raise CheckoutConfigurationError("checkout_successor_amendment_request_unverified")
    deal = IgDeal.objects.select_for_update().get(pk=receipt.proposal.deal_id, client=locked_client)
    origin = IgCheckoutProposal.objects.select_for_update().get(pk=receipt.proposal_id, deal=deal)
    from management.services.ig_checkout_cart_amendments import assert_pending_prebank
    if cancelled:
        if verify_cancelled_amendment(receipt.pk).get("ready") is not True:
            raise CheckoutConfigurationError("checkout_cancelled_scope_changed")
    else:
        assert_pending_prebank(origin, receipt)
    execution = getattr(origin, "assisted_commerce_execution", None)
    if execution is not None:
        execution = type(execution).objects.select_for_update().get(pk=execution.pk)
    successor = IgCheckoutProposal.objects.create(client=locked_client, deal=deal,
        commercial_episode=origin.commercial_episode, catalog_total=quote.catalog_total,
        negotiated_discount=quote.negotiated_discount, quoted_total=quote.quoted_total,
        requested_payment_amount=quote.requested_payment_amount, pay_type=quote.pay_type,
        allow_promo=False, items_digest=quote.digest, expires_at=origin.expires_at,
        locale=origin.locale)
    if not cancelled:
        origin.status = origin.Status.SUPERSEDED
        origin.superseded_by = successor
        origin.save(update_fields=["status", "superseded_by", "updated_at"])
    if execution is not None and not cancelled:
        if execution.active_slot != 1 or execution.order_id or execution.released_at or execution.settled_at:
            raise CheckoutConfigurationError("checkout_original_execution_terminal")
        execution.active_slot = None
        execution.released_at = timezone.now()
        execution.release_reason = "superseded"
        execution.save(update_fields=["active_slot", "released_at", "release_reason"])
    from management.services.ig_inventory import release_proposal_inventory
    release_proposal_inventory(origin, reason=f"amendment_successor:{receipt.pk}")
    deal.active_checkout_proposal = None
    deal.save(update_fields=["active_checkout_proposal", "updated_at"])
    retired = _RetiredAmendmentProposal(locked_client.pk, deal.pk, origin.pk, receipt.pk,
        receipt.source_hmac, origin.expires_at, origin.status, successor.pk, _AMENDMENT_SUCCESSOR_ISSUER)
    # The ordinary engine proves the new request/decision, prices, options and
    # stock again; the private witness only admits the exact retired predecessor.
    return create_or_update_proposal(client=locked_client, deal=deal, pay_type="online_full", item_specs=[],
        verified_assisted_decision=verified_assisted_decision, verified_selection=verified_selection,
        _retired_amendment_proposal=retired, allow_promo=False, locale=locale)
