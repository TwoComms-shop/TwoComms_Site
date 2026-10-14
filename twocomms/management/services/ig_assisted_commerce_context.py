"""Current typed manager facts for the canonical live revision boundary.

Only the native execution reader supplies an offer. Raw operator DTOs, drafts,
model controls, paid receipt text and historical terms cannot create this frame.
"""
from copy import deepcopy
from types import SimpleNamespace
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
import json
import re

from management.services.ig_assisted_commerce import VerifiedAssistedDecision

CLAIM = "assisted_commerce_offer"
ACTION = "assisted_checkout_proposal_create"
_ISSUER = object()

_DELIVERY_COMPONENT = re.compile(r"достав\w*|перевізник\w*|перевозчик\w*|\b(?:delivery|shipping|carrier|parcel|shipment)\b", re.I)
_DELIVERY_PHASES = {
    "now": re.compile(r"\b(?:зараз|сейчас|now)\b|готов\w*\s+(?:част\w*|позиц\w*|товар\w*)|(?:ready|selected)\s+(?:part|items|goods)|(?:ця|це|эта|этот|this|current)\s+(?:відправ\w*|отправ\w*|посил\w*|посыл\w*|shipment|parcel)", re.I),
    "later": re.compile(r"пізніш\w*|позже|решт\w*|залишк\w*|оставш\w*|\b(?:later|remaining)\b|(?:наступ\w*|следующ\w*|next)\s+(?:відправ\w*|отправ\w*|посил\w*|посыл\w*|shipment|parcel)", re.I),
    "together": re.compile(r"\b(?:разом|вместе|together)\b|(?:all|усі|всі|все)\s+(?:items|goods|позиції|товари|позиции|товары)", re.I),
}
_DELIVERY_CAP = re.compile(r"\bдо\b|\bup\s+to\b|максим\w*|\bmaximum\b|\bcap\b|не\s+більш\w*|не\s+бол\w*|at\s+most|no\s+more\s+than", re.I)
_DELIVERY_ACTUAL = re.compile(r"фактич\w*|фактичес\w*|\bactual\b|тариф\w*|\b(?:tariff|quote|quoted|estimate|estimated)\b|(?:достав\w*|delivery|shipping)\s+(?:кошту\w*|стоит|costs?\b)|(?:перевізник\w*|перевозчик\w*|\bcarrier\b)[^,;.]{0,20}(?:стяг\w*|візьм\w*|возьм\w*|charges?\b|bills?\b)", re.I)
_DELIVERY_PROMOTION = re.compile(r"акці\w*|акци\w*|промо\w*|\b(?:promo\w*|promotion\w*|discount\w*)\b", re.I)
_DELIVERY_FREE = re.compile(r"безкоштов\w*|бесплат\w*|\bfree\b", re.I)
_DELIVERY_MERCHANT = re.compile(r"(?:twocomms|магазин\w*|\bstore\b|\bmerchant\b|\bwe\b|\bми\b|\bмы\b)[^,;.]{0,35}(?:опла\w*|спла\w*|плат\w*|бер\w*\s+на\s+себе|\bpay\w*|\bcover\w*)|(?:paid|covered)\s+by\s+(?:twocomms|the\s+store|us)|(?:опла\w*|спла\w*)[^,;.]{0,20}(?:магазин\w*|twocomms)", re.I)
_DELIVERY_CUSTOMER = re.compile(r"(?:\bви\b|\bвы\b|клієнт\w*|клиент\w*|одержувач\w*|отримувач\w*|получател\w*|\byou\b|\bcustomer\b|\brecipient\b)[^,;.]{0,25}(?:опла\w*|спла\w*|плат\w*|\bpay\w*)|(?:оплат\w*|оплач\w*|сплач\w*|\bpaid\b|\bpay\w*)[^,;.]{0,35}(?:окремо|отдельно|separately)", re.I)
_DELIVERY_CUSTOMER_ZERO = re.compile(r"для\s+вас|\bfor\s+you\b|\byou\s+pay\b|\bви\s+(?:плат\w*|опла\w*|спла\w*)|\bвы\s+(?:плат\w*|опла\w*)", re.I)
_DELIVERY_INCLUDED = re.compile(r"(?:включ\w*|вход\w*)[^,;.]{0,35}(?:сум\w*|оплат\w*|цін\w*|цен\w*)|\b(?:prepaid|included|already\s+paid)\b|передоплач\w*|предоплач\w*|вже\s+оплач\w*|уже\s+оплач\w*", re.I)


@dataclass(frozen=True)
class AssistedDeliveryAuthority:
    """An adopted logistics term; never a goods price or actual carrier bill."""
    phase: str
    payer: str
    customer_maximum: Decimal
    currency: str
    source_ref: str


def _native_delivery_terms(decision, verified):
    """Reuse the native reader's exact signed decision, never a draft DTO."""
    from management.services import ig_assisted_commerce as commerce
    from management.services.ig_assisted_scopes import normalize_shipping_terms
    if (not isinstance(verified, VerifiedAssistedDecision) or verified.historical
        or not commerce.verify_assisted_evidence(decision, "decision")
        or decision.pk != verified.decision_id or decision.request_id != verified.request_id
        or decision.client_id != verified.client_id or decision.version != verified.decision_version
        or decision.status != "approved" or decision.request_digest != verified.request_digest
        or decision.envelope_hmac != verified.decision_digest or decision.snapshot_digest != verified.snapshot_digest
        or decision.full_capture_digest != verified.full_capture_digest):
        return None
    quote = (decision.envelope["snapshot"].get("scoped_quotes") or {}).get("selected")
    if not isinstance(quote, dict) or quote.get("delivery_scope") not in {"now", "later", "together"}:
        return None
    try:
        terms = normalize_shipping_terms(quote.get("shipping_terms"))
    except commerce.AssistedDecisionError:
        return None
    return {"terms": terms, "delivery_scope": quote["delivery_scope"], "source_ref": verified.decision_digest}


def _positive(pattern, text):
    from management.services.ig_reply_truth import _locally_negated
    return any(not _locally_negated(text, match.start()) for match in pattern.finditer(text))


def _delivery_units(text, lines):
    """Separate stated money components without splitting decimal commas."""
    from management.services.ig_reply_truth import _claim_sentences
    starts = r"достав\w*|ціна\w*|цена\w*|товар\w*|delivery|shipping|price|goods|merchandise|total"
    starts += "|" + "|".join(re.escape(line.title) for line in lines if line.title)
    boundary = re.compile(r"(?<!\d),(?!\d)|;|\s+(?:і|й|та|а|и|and|but)\s+(?=(?:" + starts + r")\b)", re.I)
    for sentence in _claim_sentences(text, ()):
        if not _DELIVERY_COMPONENT.search(sentence):
            # Commas in "Hoodie available, size L, price 850" do not start a
            # new anonymous product. Keep the original exact subject unit.
            yield sentence, False
            continue
        pieces, start = [], 0
        for separator in boundary.finditer(sentence):
            current, following = sentence[start:separator.start()], sentence[separator.end():].lstrip()
            delivery_current = bool(_DELIVERY_COMPONENT.search(current))
            named_following = any(line.title and following.casefold().startswith(line.title.casefold()) for line in lines)
            domain_switch = bool(_DELIVERY_COMPONENT.match(following)) or delivery_current and (
                named_following or re.match(r"ціна\w*|цена\w*|товар\w*|price|goods|merchandise|total", following, re.I)
                or any(pattern.match(following) for pattern in _DELIVERY_PHASES.values()))
            if domain_switch:
                pieces.append(current)
                start = separator.end()
        pieces.append(sentence[start:])
        delivery_seen = False
        for unit in pieces:
            unit = unit.strip()
            if not unit:
                continue
            explicit = bool(_DELIVERY_COMPONENT.search(unit))
            phased = any(pattern.search(unit) for pattern in _DELIVERY_PHASES.values())
            goods = re.search(r"ціна\w*|цена\w*|товар\w*|\b(?:price|goods|merchandise|total)\b", unit, re.I)
            named = any(line.title and re.search(re.escape(line.title), unit, re.I) for line in lines)
            # Only a plain continuation phase label may inherit "delivery".
            # A new goods component must never borrow its preceding cap.
            continuation = phased or any(pattern.search(unit) for pattern in (
                _DELIVERY_CAP, _DELIVERY_ACTUAL, _DELIVERY_PROMOTION, _DELIVERY_FREE,
                _DELIVERY_MERCHANT, _DELIVERY_CUSTOMER, _DELIVERY_INCLUDED))
            is_delivery = explicit or delivery_seen and continuation and not goods and not named
            delivery_seen = delivery_seen or explicit
            yield unit, is_delivery


def should_capture_assisted_context(plan, client, revision=None):
    """No extra lookup on a ready ordinary catalogue path."""
    if revision is not None and any(
        row.get("role") == "user" and str(row.get("quick_reply_payload") or "").startswith("AC1:")
        for row in (revision.bundle_snapshot or {}).get("sources", ())
    ):
        # This is only a bounded read hint. Native delivery, source ownership,
        # signed choice and exact selected scope are verified by the reader.
        return True
    if "custom" in str(getattr(client, "intent", "") or "").casefold():
        return True
    rows = [line.as_dict() for line in plan.line_plans] or [plan.as_dict()]
    for row in rows:
        choices, state = row.get("choices") or {}, row.get("configuration") or {}
        missing = state.get("missing") or []
        if (state.get("requested_size_unavailable") or (choices.get("color") and "color" in missing)
            or (choices.get("fit_option_code") and "fit" in missing)
            or (choices.get("model_query") and not choices.get("product_id"))):
            return True
    if revision is not None:
        from management.services.ig_product_inquiries import product_inquiries_for_revision
        return product_inquiries_for_revision(revision, response_plan=plan).get("status") == "manager_required"
    return False


@dataclass(frozen=True)
class AssistedCommerceContext:
    verified: VerifiedAssistedDecision
    revision_id: int
    revision_digest: str
    cart_capture_digest: str
    can_prepare_checkout: bool
    _issuer: object
    selection: object | None = None
    partial_pending: bool = False
    _current_capture_json: str | None = None
    _delivery_terms_json: str | None = None

    def __post_init__(self):
        if (self._issuer is not _ISSUER or not isinstance(self.verified, VerifiedAssistedDecision)
            or self.verified.proof_scope == "historical"):
            raise ValueError("assisted_context_requires_current_native_reader")
        if self._delivery_terms_json is not None:
            from management.services.ig_assisted_scopes import normalize_shipping_terms
            native = json.loads(self._delivery_terms_json)
            if (set(native) != {"terms", "delivery_scope", "source_ref"}
                or native["source_ref"] != self.verified.decision_digest
                or native["delivery_scope"] not in {"now", "later", "together"}
                or normalize_shipping_terms(native["terms"]) != native["terms"]):
                raise ValueError("assisted_delivery_terms_require_exact_native_decision")
        if self.selection is not None:
            from management.services.ig_assisted_scopes import VerifiedAssistedSelection, selection_evidence
            if (type(self.selection) is not VerifiedAssistedSelection or self.selection.historical
                or any(getattr(self.selection, key) != getattr(self.verified, key)
                    for key in ("client_id", "request_id", "decision_id", "request_digest", "decision_digest", "snapshot_digest"))):
                raise ValueError("assisted_context_requires_current_native_selection")
            selection_evidence(self.selection)
        elif self.partial_pending and self.can_prepare_checkout:
            raise ValueError("assisted_partial_context_requires_native_choice")

    @property
    def current_capture(self):
        if self._current_capture_json is not None:
            return json.loads(self._current_capture_json)
        return self.selection.current_capture if self.selection is not None else self.verified.current_capture

    @property
    def lines(self):
        if self.selection is not None:
            selected = set(self.selection.current_selected_line_ids)
            deferred = tuple(replace(line, unit_price=None) for line in self.verified.lines if line.line_id not in selected)
            return (*self.selection.lines, *deferred)
        if self.partial_pending:
            return tuple(replace(line, unit_price=None) for line in self.verified.lines)
        return self.verified.lines

    @property
    def approved_total(self):
        if self.selection is not None:
            return self.selection.approved_total
        return None if self.partial_pending else self.verified.approved_total

    def _current_line_id(self, line):
        if self.selection is not None:
            return next((row["current_line_id"] for row in self.selection.line_map if row["line_id"] == line.line_id), line.line_id)
        return line.line_id

    @property
    def delivery_authorities(self):
        # Selection and full-context terms share native signed decision
        # ownership. Neither supplies actual carrier bills or promo approval.
        native = json.loads(self._delivery_terms_json) if self._delivery_terms_json is not None else None
        if self.selection is None and native is None:
            return ()
        terms = self.selection.shipping_terms if self.selection is not None else native["terms"]
        source_ref = self.selection.terms_digest if self.selection is not None else native["source_ref"]
        result = []
        for phase in ("now", "later", "together"):
            term = terms.get(phase) if isinstance(terms, dict) else None
            if (not isinstance(term, dict) or term.get("payer") not in {"customer", "merchant"}
                or term.get("currency") != "UAH" or not isinstance(term.get("customer_maximum"), str)):
                return ()
            try:
                maximum = Decimal(term["customer_maximum"])
            except InvalidOperation:
                return ()
            if not maximum.is_finite() or maximum < 0 or term["payer"] == "merchant" and maximum != 0:
                return ()
            result.append(AssistedDeliveryAuthority(phase, term["payer"], maximum, "UAH", source_ref))
        return tuple(result)

    @property
    def delivery_scope(self):
        if self.selection is not None:
            return self.selection.delivery_scope
        native = json.loads(self._delivery_terms_json) if self._delivery_terms_json is not None else {}
        return native.get("delivery_scope", "")

    def _checked_delivery_copy(self, text):
        """Mask only proven delivery money, leaving every other claim intact."""
        from management.services.ig_reply_truth import (_MONEY_RE, _PREFIX_MONEY_RE, _RANGE_RE,
            _currency_code, _decimal, ReplyTruthResult)
        authorities = {fact.phase: fact for fact in self.delivery_authorities}
        masked, checked_units, preserved_zeroes, cursor = list(text), [], [], 0
        for unit, is_delivery in _delivery_units(text, self.lines):
            offset = text.find(unit, cursor)
            if offset < 0:
                return text, (), ReplyTruthResult(False, ("assisted_delivery_scope_unverified",))
            cursor = offset + len(unit)
            money = sorted({(match.start(), match.end()): match for pattern in (_MONEY_RE, _PREFIX_MONEY_RE)
                for match in pattern.finditer(unit)}.values(), key=lambda match: match.start())
            delivery_money = []
            component = _DELIVERY_COMPONENT.search(unit)
            if is_delivery:
                for match in money:
                    if component and match.end() <= component.start():
                        continue  # A named goods price before the delivery component.
                    goods = [found for line in self.lines for found in re.finditer(re.escape(line.title), unit[:match.start()], re.I)
                        if line.title and (not component or found.start() > component.start())]
                    if goods and re.fullmatch(r"\s*[:—–-]?\s*(?:(?:ціна|цена|price)\s*[:—–-]?\s*)?",
                            unit[max(goods, key=lambda found: found.start()).end():match.start()], re.I):
                        continue
                    delivery_money.append(match)
                if _positive(_DELIVERY_PROMOTION, unit) or _positive(_DELIVERY_INCLUDED, unit):
                    return text, (), ReplyTruthResult(False, ("assisted_delivery_terms_unverified",))
                merchant = _positive(_DELIVERY_MERCHANT, unit) or _positive(_DELIVERY_FREE, unit)
                customer = _positive(_DELIVERY_CUSTOMER, unit)
                phases = [phase for phase, pattern in _DELIVERY_PHASES.items() if pattern.search(unit)]
                needs_phase = bool(delivery_money or merchant or customer and not component and self.selection is not None)
                if needs_phase and len(phases) != 1:
                    return text, (), ReplyTruthResult(False, ("assisted_delivery_scope_unverified",))
                authority = authorities.get(phases[0]) if len(phases) == 1 else None
                if needs_phase and authority is None:
                    return text, (), ReplyTruthResult(False, ("assisted_delivery_terms_unverified",))
                if merchant and authority.payer != "merchant":
                    return text, (), ReplyTruthResult(False, ("assisted_delivery_payer_unverified",))
                if customer:
                    current = authority or authorities.get(self.delivery_scope)
                    zero_for_customer = bool(delivery_money and _DELIVERY_CUSTOMER_ZERO.search(unit)
                        and all(_decimal(match.group("amount")) == 0 for match in delivery_money))
                    if current is not None and current.payer != "customer" and not zero_for_customer:
                        return text, (), ReplyTruthResult(False, ("assisted_delivery_payer_unverified",))
                if delivery_money:
                    if (_RANGE_RE.search(unit) or _positive(_DELIVERY_ACTUAL, unit)
                        or any(re.search(r"\d\s*[-–—]\s*$", unit[:match.start()]) for match in delivery_money)):
                        return text, (), ReplyTruthResult(False, ("assisted_carrier_fee_unverified",))
                    cap = bool(_DELIVERY_CAP.search(unit))
                    customer_zero = authority.payer == "merchant" and bool(_DELIVERY_CUSTOMER_ZERO.search(unit))
                    if not cap and not customer_zero:
                        return text, (), ReplyTruthResult(False, ("assisted_carrier_fee_unverified",))
                    for match in delivery_money:
                        if (_currency_code(match.group("currency")) != authority.currency
                            or _decimal(match.group("amount")) != authority.customer_maximum):
                            return text, (), ReplyTruthResult(False, ("assisted_delivery_amount_unverified",))
                        masked[offset + match.start():offset + match.end()] = " " * (match.end() - match.start())
                        if customer_zero:
                            preserved_zeroes.append((offset + match.start(), offset + match.end()))
            checked_units.append("".join(masked[offset:offset + len(unit)]))
        # Keep the typed customer-zero qualifier for the final shipping payer
        # guard. Goods-line checks above still receive masked delivery money.
        for start, end in preserved_zeroes:
            masked[start:end] = text[start:end]
        return "".join(masked), tuple(checked_units), ReplyTruthResult(True)

    @property
    def reference(self):
        value = self.verified
        reference = {"request_id": str(value.request_id), "decision_id": str(value.decision_id),
            "request_digest": value.request_digest, "decision_digest": value.decision_digest,
            "snapshot_digest": value.snapshot_digest, "intention_id": str(value.intention_id),
            "full_capture_digest": value.full_capture_digest, "revision_id": self.revision_id,
            "revision_digest": self.revision_digest, "cart_capture_digest": self.cart_capture_digest}
        if self.selection is not None:
            from management.services.ig_assisted_scopes import selection_evidence
            reference["selection"] = selection_evidence(self.selection)
        if self._delivery_terms_json is not None:
            reference["delivery_terms"] = json.loads(self._delivery_terms_json)
        return reference

    @property
    def projection(self):
        result = {"schema": "current-assisted-dialogue.v1", "reference": self.reference,
            "manager_checked_at": self.verified.physical_checked_at.isoformat() if self.verified.physical_checked_at else None,
            "expires_at": self.verified.expires_at.isoformat(), "currency": "UAH",
            "approved_total": str(self.approved_total) if self.approved_total is not None else None,
            "lines": [line.as_dict() for line in self.lines]}
        if self.selection is not None or self.partial_pending:
            selected = set(self.selection.selected_line_ids) if self.selection is not None else set()
            result.update(schema="current-assisted-partial-dialogue.v1", scope="partial",
                native_purchase_accepted=self.selection is not None,
                selected_lines=[line.as_dict() for line in self.lines if line.line_id in selected],
                deferred_unpaid_lines=[{**line.as_dict(), "disposition": "unpaid_wishlist", "unit_price": None}
                    for line in self.lines if line.line_id not in selected],
                can_prepare_checkout=self.can_prepare_checkout)
            if self.selection is not None:
                result.update(coverage=self.selection.coverage, shipping_terms=self.selection.shipping_terms,
                    delivery_scope=self.selection.delivery_scope)
        return result

    def prompt_guidance(self):
        # Typed manager terms authorize this exact offer; they do not rewrite
        # customer choices, grant a catalogue ID or settle any payment.
        partial = ("\nOnly selected_lines belong to the accepted current purchase and its quoted goods total. "
            "deferred_unpaid_lines remain an unpaid wishlist: no price, payment, reservation, shipment or arrival claim. "
            "For delivery, name its exact phase (now/ready items, later/remaining items, or together). "
            "Shipping maxima are agreed customer caps paid separately to the carrier, never goods prices or actual carrier fees. "
            "State a maximum only with its adopted phase, payer and exact value. Merchant-funded delivery needs that phase's merchant term; no promotion or prepayment is implied. "
            "Offer help with shipping naturally; only the exact arrival date depends on the carrier. "
            "Without native_purchase_accepted there is no selected price or checkout authority."
            if self.selection is not None or self.partial_pending else "")
        return ("[CURRENT VERIFIED MANAGER OFFER]\n" + json.dumps(self.projection, ensure_ascii=False, separators=(",", ":"))
            + "\nUse these exact manager-approved offer facts for the named current lines. READY is checked physical availability in the stated quantity. "
            "Manager terms differing from a source wish are a counteroffer; the hosted checkout must obtain customer adoption. "
            "Only confirmed answers to the exact original question are known; new fabric/hardware questions remain unanswered. "
            "A typed preparation/dispatch estimate is its own domain and never guarantees arrival. No typed estimate means timing unknown. "
            "Do not create product IDs, stock, discounts, paid money or order-created facts. Reference IDs are backend metadata, never customer prose." + partial)

    def resolves_inquiry(self, inquiry):
        source = self.verified.source_proof
        questions = source.get("questions") or []
        answers = {answer["question_id"]: answer for line in self.lines
                   for answer in line.as_dict()["question_answers"]}
        keys = ("source_message_id", "source_digest", "line_id", "axis", "requested_value", "question")
        return any(all(question.get(key) == inquiry.get(key) for key in keys)
            and answers.get(question.get("question_id"), {}).get("state") == "confirmed" for question in questions)

    def purchase_ready(self, revision, capture):
        from management.services.ig_response_cart_plan import source_line_preferences
        if self.selection is not None:
            proof = self.selection.purchase_proof
            current_ids = {row["message_id"] for row in revision.bundle_snapshot.get("sources", ()) if row.get("role") == "user"}
            return bool(self.can_prepare_checkout and self.selection.lines
                and self.selection.revision_id == revision.pk and self.selection.purchase_source_id in current_ids
                and proof.get("revision_digest") == revision.snapshot_digest
                and capture.get("capture_digest") == self.cart_capture_digest)
        if self.partial_pending:
            return False
        if not self.can_prepare_checkout or not self.verified.lines or len(self.verified.ready_lines) != len(self.verified.lines):
            return False
        current_ids = {row["message_id"] for row in revision.bundle_snapshot.get("sources", ()) if row.get("role") == "user"}
        rows = capture.get("lines") or []
        if {row.get("line_id") for row in rows} != {line.line_id for line in self.verified.lines}:
            return False
        return all((value := source_line_preferences(capture, row)).get("values", {}).get("purchase_requested") is True
            and value.get("evidence", {}).get("purchase_requested", {}).get("source_message_id") in current_ids for row in rows)

    def _line_context(self, base, line):
        from management.services.ig_reply_truth import TimingAuthority
        from management.services.ig_product_inquiries import _color
        typed = line.as_dict()
        times = []
        for domain in ("preparation", "dispatch"):
            timing = typed[domain]
            if timing and line.availability in {"ready", "wait"}:
                times.append(TimingAuthority(domain=domain, min_days=timing["min_days"], max_days=timing["max_days"],
                    unit="calendar_days" if timing["unit"] == "calendar" else "business_days",
                    anchor_at="",
                    line_id=self._current_line_id(line), line_label=line.title, source_ref=self.verified.decision_digest,
                    review_line_labels=tuple((self._current_line_id(value), value.title) for value in self.lines)))
                if len(self.lines) == 1:
                    times.append(replace(times[-1], line_id="", line_label=""))
        return replace(base, authorized_prices=(line.unit_price,) if line.unit_price is not None else (),
            authorized_price_ranges=(), allowed_sizes=(line.size,), allowed_fits=(line.fit_code,) if line.fit_code else (),
            allowed_colors=tuple(dict.fromkeys((line.color, _color(line.color)))),
            timing_authorities=tuple(times), approved_timing_claims=(), explicitly_qualified_standard_dispatch_days=None,
            availability_authorities=(), fulfillment_review_status="")

    def validate_reply(self, response, base):
        from management.services.ig_reply_truth import (_claim_sentences, _MONEY_RE, _PREFIX_MONEY_RE, _product_availability_claim,
            validate_reply_truth, ReplyTruthResult, _TIMING_RE, _timing_nonclaim)
        lines = self.lines
        checked_reply, checked_units, delivery_result = self._checked_delivery_copy(response.reply_text)
        if not delivery_result.valid:
            return delivery_result
        from management.services.ig_reply_truth import ShippingAuthority
        local_shipping = tuple(ShippingAuthority(fact.source_ref,
            "merchant_free" if fact.payer == "merchant" else "carrier_recipient",
            "Sender" if fact.payer == "merchant" else "Recipient",
            "ig_assisted_selected" if self.selection is not None else "ig_assisted_manager",
            phase=fact.phase, customer_maximum=fact.customer_maximum,
            customer_charge=Decimal("0") if fact.payer == "merchant" else None)
            for fact in self.delivery_authorities)
        if not local_shipping and self.selection is None and not self.partial_pending:
            # Ordinary carrier payment is a no-fee business rule. It cannot
            # establish a cap, merchant payer, prepaid charge or promotion.
            local_shipping = (ShippingAuthority(self.verified.decision_digest,
                "carrier_recipient", "Recipient", "ordinary_carrier_payment"),)
        if local_shipping:
            base = replace(base, shipping_authorities=local_shipping)
        from management.services.ig_product_inquiries import _SPEC, validate_inquiry_response
        from management.services.ig_reply_language import own_reply_text
        questions = {question["question_id"]: question for question in self.verified.source_proof.get("questions", ())
                     if question.get("axis") == "information"}
        def exact_unit(text):
            return re.sub(r"\s+", " ", text.strip().rstrip(".!; ")).casefold().replace("ё", "е")

        for part in _claim_sentences(own_reply_text(response.reply_text), ()):
            material = re.search(r"метал\w*|\bmetal\w*\b|пластик\w*|\bplastic\b|хлоп\w*|бавовн\w*|\bcotton\b|поліестер\w*|полиэстер\w*|\bpolyester\b|\b\d{1,3}\s*%", part, re.I)
            if not (_SPEC.search(part) or material) or "?" in part:
                continue
            named = [line for line in lines if re.search(re.escape(line.title), part, re.I)
                     or re.search(r"(?:поз\w*|позици\w*|item|position)\s*#?\s*" + str(line.original_index + 1) + r"(?!\d)", part, re.I)]
            scoped = named if len(named) == 1 else ([lines[0]] if not named and len(lines) == 1 else [])
            approved = False
            for line in scoped:
                # A framing label identifies the answer's subject. Its entire
                # remaining asserted unit must be verbatim approved evidence;
                # a substring cannot license an appended or borrowed fact.
                framed = re.sub(r"^\s*(?:" + re.escape(line.title)
                    + r"|(?:поз\w*|позици\w*|item|position)\s*#?\s*" + str(line.original_index + 1)
                    + r"(?!\d))\s*[:—–-]\s*", "", part, count=1, flags=re.I)
                for answer in line.as_dict()["question_answers"]:
                    question = questions.get(answer["question_id"], {})
                    if (answer["state"] == "confirmed" and question.get("line_id") == self._current_line_id(line)
                        and answer["answer"].strip() and exact_unit(framed) == exact_unit(answer["answer"])):
                        approved = True
                        break
            if not approved:
                honest = validate_inquiry_response({"status": "manager_required", "inquiries": [{"axis": "information"}]},
                    SimpleNamespace(control={"manager": True}, reply_text=part))
                if honest:
                    return ReplyTruthResult(False, ("product_information_unverified",))
        for clause in checked_units:
            matched = [line for line in lines if re.search(re.escape(line.title), clause, re.I)
                or re.search(r"(?:поз\w*|позици\w*|item|position)\s*#?\s*" + str(line.original_index + 1) + r"(?!\d)", clause, re.I)]
            money = bool(_MONEY_RE.search(clause) or _PREFIX_MONEY_RE.search(clause))
            stock = _product_availability_claim(clause)
            timing = any(not _timing_nonclaim(clause, match.start()) for match in _TIMING_RE.finditer(clause))
            if not (money or stock or timing):
                continue
            total = money and re.search(r"разом|итого|всього|total|весь\s+(?:кошик|заказ)", clause, re.I)
            if money and (self.selection is not None or self.partial_pending):
                selected_total = re.search(r"(?:готов\w*|вибран\w*|выбран\w*)\s+(?:част\w*|позиц\w*|товар\w*)|(?:ready|selected)\s+(?:part|items|goods)", clause, re.I)
                total = total or selected_total
                if total:
                    selected = set(self.selection.selected_line_ids) if self.selection is not None else set()
                    if (self.approved_total is None or re.search(r"весь\s+(?:кошик|заказ)|ус[іi]\s+товар|all\s+(?:items|goods)", clause, re.I)
                        or any(line.line_id not in selected for line in matched)
                        or not matched and not selected_total):
                        return ReplyTruthResult(False, ("assisted_partial_total_scope_unverified",))
            if not matched and len(lines) == 1:
                matched = [lines[0]]
            if total and not stock and not timing:
                result = validate_reply_truth(clause, context=replace(base, authorized_prices=(self.approved_total,) if self.approved_total is not None else (),
                    authorized_price_ranges=(), explicitly_qualified_standard_dispatch_days=None, approved_timing_claims=()))
            elif len(matched) != 1:
                return ReplyTruthResult(False, ("assisted_reply_line_scope_unverified",))
            else:
                line = matched[0]
                if stock and (line.availability != "ready" or type(line.checked_quantity) is not int or line.checked_quantity < line.quantity):
                    return ReplyTruthResult(False, ("unverified_availability",))
                if stock:
                    from management.services.ig_commerce_turns import _COLOR_WORDS, _word_pattern, _SIZE_RE, _canonical_source_size
                    from management.services.ig_product_inquiries import _color
                    from management.services.ig_reply_truth import _STOCK_QUANTITY_RE, _locally_negated
                    for needle, color in _COLOR_WORDS.items():
                        if any(not _locally_negated(clause, match.start()) for match in
                               re.finditer(_word_pattern(needle), clause.replace("ё", "е"), re.I)) and color != _color(line.color):
                            return ReplyTruthResult(False, ("configuration_mismatch",))
                    if any(not _locally_negated(clause, match.start()) and _canonical_source_size(match.group()) != _canonical_source_size(line.size)
                           for match in _SIZE_RE.finditer(clause)):
                        return ReplyTruthResult(False, ("configuration_mismatch",))
                    if any(int(first or second) != line.quantity for first, second in _STOCK_QUANTITY_RE.findall(clause)):
                        return ReplyTruthResult(False, ("unverified_availability",))
                result = validate_reply_truth(clause, context=self._line_context(base, line))
            if not result.valid:
                return result
        contexts = [self._line_context(base, line) for line in lines]
        prices = ([self.approved_total] if self.approved_total is not None else []) + [line.unit_price for line in lines if line.unit_price is not None]
        aggregate = replace(base, authorized_prices=tuple(dict.fromkeys(prices)),
            allowed_sizes=tuple(dict.fromkeys(line.size for line in lines)),
            allowed_colors=tuple(dict.fromkeys(color for context in contexts for color in context.allowed_colors)),
            allowed_fits=tuple(dict.fromkeys(line.fit_code for line in lines if line.fit_code)),
            timing_authorities=tuple(fact for context in contexts for fact in context.timing_authorities),
            authorized_price_ranges=(), approved_timing_claims=(), explicitly_qualified_standard_dispatch_days=None,
            availability_authorities=(), fulfillment_review_status="")
        return validate_reply_truth(checked_reply, context=aggregate)

    def coverage(self, plan, response, revision, original):
        """Credit only current typed offer facts; payment/support debt remains."""
        from management.services.ig_reply_truth import ReplyTruthContext, _MONEY_RE
        if not self.validate_reply(response, ReplyTruthContext()).valid:
            return original
        result = deepcopy(original)
        additions = []
        native_purchase = False
        if self.selection is not None:
            from management.services.ig_revision_checkout import _payment_request
            request, reasons = _payment_request(response.control)
            native_purchase = bool((response.control.get("paylink") or response.control.get("payment"))
                and request == "full" and not reasons
                and not any(key in response.control for key in
                    ("items", "item", "product", "color_variant_id", "option", "price", "price_quoted"))
                and self.purchase_ready(revision, plan.source_cart_capture))
        for obligation in plan.obligations:
            if obligation["id"] not in result.get("remaining", ()):
                continue
            kind = obligation["kind"]
            if (native_purchase and kind == "unresolved_line_operation"
                and not obligation.get("line_id")
                and obligation.get("source_message_id") == self.selection.purchase_source_id
                and obligation["id"] == f"{self.selection.purchase_source_id}:unresolved_line_operation"):
                # The real AC1 action is already an owned selected purchase.
                # It supplies no fabricated cart-field observations and cannot
                # acknowledge a different source's unresolved operation.
                additions.append(obligation["id"])
                continue
            if self.selection is not None and obligation.get("line_id") not in set(self.selection.current_selected_line_ids):
                continue
            if self.partial_pending:
                continue
            if kind == "info:price" and _MONEY_RE.search(response.reply_text):
                additions.append(obligation["id"])
            elif kind == "purchase_requested" and (response.control.get("paylink") or response.control.get("payment")) and self.purchase_ready(revision, plan.source_cart_capture):
                additions.append(obligation["id"])
        result["remaining"] = [item for item in result.get("remaining", ()) if item not in additions]
        result["covered"] = list(dict.fromkeys([*result.get("covered", ()), *additions]))
        if not result["remaining"]:
            result["disposition"] = "complete"
        return result


def capture_assisted_context(client, revision, plan=None, *, reference=None, now=None):
    if reference is None and (plan is None or not should_capture_assisted_context(plan, client, revision)):
        return None, "assisted_context_not_applicable"
    from management.services.ig_assisted_checkout import current_assisted_offer
    result = current_assisted_offer(client, revision=revision, now=now)
    if not isinstance(result, dict) or not result.get("ready"):
        return None, (result.get("reason") if isinstance(result, dict) else "") or "assisted_offer_unavailable"
    value = result.get("verified_decision")
    if not isinstance(value, VerifiedAssistedDecision) or value.client_id != client.pk or value.proof_scope == "historical":
        return None, "assisted_offer_unverified"
    selection = result.get("verified_selection")
    if selection is not None:
        from management.services.ig_assisted_scopes import VerifiedAssistedSelection
        if (type(selection) is not VerifiedAssistedSelection or selection.historical
            or selection.client_id != client.pk):
            return None, "assisted_selection_unverified"
    decision = result.get("decision")
    partial_pending = bool(selection is None and decision is not None
        and ((decision.envelope.get("snapshot") or {}).get("scoped_quotes") or {}).get("selected"))
    current = result.get("current_capture") or value.current_capture
    capture = plan.source_cart_capture if plan is not None else current
    # Only the native execution reader may explain its own quote-owner change.
    # Source choices, approval refs and revision are still exact.
    from management.services.ig_assisted_checkout import VerifiedAssistedExecution
    execution = result.get("verified_execution")
    before = (result.get("source_cart_binding") or {}).get("current_capture") or {}
    if isinstance(execution, VerifiedAssistedExecution) and execution.client_id == client.pk:
        if capture.get("capture_digest") == before.get("capture_digest"):
            capture = current
    context = AssistedCommerceContext(value, revision.pk, revision.snapshot_digest, capture.get("capture_digest", ""),
        result.get("can_prepare_checkout") is True, _ISSUER, selection=selection, partial_pending=partial_pending,
        _current_capture_json=json.dumps(current, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        _delivery_terms_json=json.dumps(terms, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if selection is None and (terms := _native_delivery_terms(decision, value)) is not None else None)
    if reference is not None and context.reference != reference:
        rebound = dict(reference) if isinstance(reference, dict) else {}
        if (isinstance(execution, VerifiedAssistedExecution) and execution.client_id == client.pk
            and rebound.get("cart_capture_digest") == before.get("capture_digest")):
            rebound["cart_capture_digest"] = context.cart_capture_digest
        if rebound != context.reference:
            return None, "assisted_offer_changed"
    return context, ""
