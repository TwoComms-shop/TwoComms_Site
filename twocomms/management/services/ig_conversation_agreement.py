"""Source-bound conversation wishes and seller quotes, without payment effects.

The input is an ordered, bounded transcript of persisted message rows.  A
customer's short confirmation can accept a seller's explicit configuration;
ordinary mentions and an image by itself cannot create a catalogue identity.
Money here is a quoted instruction, never a payment receipt or paid status.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re


SCHEMA = "conversation-agreement.v1"
MAX_MESSAGES = 160
_SELLERS = {"manager", "human_manager", "operator", "admin"}
_CUSTOMERS = {"user", "customer", "client"}
_BOTS = {"model", "assistant", "bot"}
_CURRENCY = r"(?:грн\.?|uah|₴|usd|\$|eur|€)"
_NUMBER = r"\d{1,6}(?:[.,]\d{1,2})?"
_AMOUNT = re.compile(r"(?<!\d)(?P<value>" + _NUMBER + r")\s*(?P<currency>" + _CURRENCY + r")", re.I)
_DELIVERY = r"(?:доставк\w*|shipping|delivery)"
_MERCHANT_SHIPPING = (r"\b(?:безкоштов\w*|бесплат\w*|безоплат\w*|free|за\s+(?:наш\w*\s+(?:рахун\w*|сч[её]т)|рахунок\s+магазину)|"
    r"(?:оплачуємо|оплачиваем)\s+(?:ми|мы)|(?:ми|мы|we|seller)\s+(?:оплачу\w*|оплачива\w*|сплачу\w*|pay|covers?))\b")
_SPLIT = re.compile(
    r"(?P<merch>" + _NUMBER + r")\s*(?:" + _CURRENCY + r")?\s*\+\s*"
    r"(?:" + _DELIVERY + r"\s*[:=-]?\s*)?(?P<delivery>" + _NUMBER + r")\s*"
    r"(?:" + _CURRENCY + r")?\s*(?:" + _DELIVERY + r")?\s*=\s*"
    r"(?P<total>" + _NUMBER + r")\s*(?P<currency>" + _CURRENCY + r")", re.I,
)
_SIZE = re.compile(r"(?<!\w)(?:xxxs|xxs|xs|[xх]{1,3}[lл]|[2-5][xх][lл]|s|m|l|л)(?!\w)", re.I)
_FIT = re.compile(r"(?<!\w)(?:оверсайз\w*|отверсаз|oversize(?:d)?|класич\w*|классич\w*|classic|regular)(?!\w)", re.I)
_COLOR = re.compile(r"(?<!\w)(?:біл\w*|бел\w*|white|чорн\w*|черн\w*|black|сір\w*|сер\w*|gr[ae]y|син\w*|blue|зелен\w*|green|рожев\w*|розов\w*|pink)(?!\w)", re.I)
_GARMENT = re.compile(r"(?<!\w)(?:футболк\w*|t-?shirt|tee|худі|худи|hoodie|лонгслів\w*|лонгслив\w*|longsleeve)(?!\w)", re.I)
_BRAND = re.compile(r"(?<!\w)two\s*com{1,2}s\b", re.I)
_MODEL_LABEL = re.compile(r"\b(?:модель|принт|назва|название|model|design|title)\s*[:=-]\s*(.{3,120})", re.I)
_NEGATIVE = re.compile(r"\b(?:не|ні|нет|not|no|don't|dont|нехочу|скас\w*|отмен\w*|cancel\w*)\b", re.I)
_RESET = re.compile(r"\b(?:інш\w*|друг(?:ой|ую|а)|another|different|замін\w*|заміни|смен\w*|replace|switch)\b", re.I)
_ACCEPT = re.compile(
    r"^(?:так|да|ок(?:ей)?|okay|ok|yes|добре|хорошо|згод(?:ен|на)|соглас(?:ен|на)|"
    r"підтверджую|подтверждаю|домовились|беру|замовляю|заказываю|оформляємо|оформляем)\b", re.I,
)


def _row(raw):
    if not isinstance(raw, dict):
        raw = {key: getattr(raw, key, None) for key in (
            "pk", "role", "text", "source", "status", "send_state", "provider_message_id",
            "provider_created_at", "created_at", "provider_namespace", "sender_id", "client_id", "attachments", "attachment_media",
            "reply_to_provider_message_id", "mid",
        )}
    result = dict(raw)
    value = raw.get("id") or raw.get("message_id") or raw.get("pk")
    result["id"] = int(value) if isinstance(value, (int, str)) and not isinstance(value, bool) and str(value).isdigit() else 0
    result["message_id"] = result["id"]
    result["role"] = str(raw.get("role") or "").casefold()
    result["text"] = str(raw.get("text") or raw.get("quote") or "")
    for key in ("provider_created_at", "created_at", "event_at"):
        if hasattr(result.get(key), "isoformat"):
            result[key] = result[key].isoformat()
    return result


def _proof(row):
    stamp = row.get("provider_created_at") or row.get("event_at") or row.get("created_at")
    if hasattr(stamp, "isoformat"):
        stamp = stamp.isoformat()
    proof = {"source_message_id": row["id"], "source_digest": hashlib.sha256(row["text"].encode()).hexdigest(),
        "role": row["role"], "source": str(row.get("source") or "persisted_transcript"),
        "provider_namespace": str(row.get("provider_namespace") or ""),
        "provider_message_id": str(row.get("provider_message_id") or row.get("mid") or ""),
        "observed_at": str(stamp or ""), "time_origin": "provider_event" if row.get("provider_created_at") else "local_observation"}
    # Render/provider augmentation is not immutable source identity. Even an
    # empty durable manifest owns this boundary; decode raw attachments then.
    binding_source = row.get("_source_media_binding")
    binding_source = binding_source if isinstance(binding_source, dict) else row
    if "attachment_media" in binding_source or "attachments" in binding_source:
        media = _media_parts({"attachment_media": binding_source.get("attachment_media"), "attachments": binding_source.get("attachments")})
    else:
        media = _media_parts(row)
    source_attachments = binding_source.get("attachments") or ""
    if media or source_attachments:
        # Classification/OCR/capture bookkeeping is mutable analysis. Bind
        # the source attachment identities and bytes, and export only a digest.
        parts = []
        for part in media:
            if not isinstance(part, dict):
                continue
            material = {key: part[key] for key in ("source_part_id", "part_id", "original_index", "type", "media_type",
                "mime", "content_hash", "provider_attachment_id", "provider_media_id", "attachment_id") if part.get(key) is not None}
            for key in ("url", "source_url"):
                if isinstance(part.get(key), str) and part[key]:
                    material[key + "_digest"] = hashlib.sha256(part[key].encode()).hexdigest()
            parts.append(material)
        binding = {"attachments_digest": hashlib.sha256(str(source_attachments).encode()).hexdigest(), "parts": parts}
        proof["media_binding_digest"] = hashlib.sha256(json.dumps(binding, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    return proof


def _seller(row):
    if row.get("status") in {"failed", "pending", "processing"} or row.get("send_state") in {"failed", "unknown", "sending"}:
        return False
    if row["role"] in _SELLERS:
        return True
    return row["role"] in _BOTS and row.get("status") == "done" and row.get("send_state") == "sent" and bool(row.get("provider_message_id"))


def _money(value, *, allow_zero=False):
    try:
        result = Decimal(str(value).replace(",", ".")).quantize(Decimal("0.01"))
    except (ValueError, InvalidOperation):
        return ""
    return str(result) if (0 <= result if allow_zero else 0 < result) and result <= Decimal("1000000") else ""


def _currency(raw):
    value = str(raw or "").casefold().rstrip(".")
    return "USD" if value in {"usd", "$"} else "EUR" if value in {"eur", "€"} else "UAH"


def _configuration(text):
    """Only one unquoted, positive value per configuration axis."""
    if "?" in text or _NEGATIVE.search(text) or re.search(r"[«»“”\"]|\b(?:або|или|or)\b", text, re.I):
        return {}, ["configuration_not_affirmed"]
    from management.services.ig_commerce_turns import (
        _canonical_source_size, _SIZE_RE, _COLOR_WORDS, _FIT_WORDS, _GARMENT_WORDS, _find_prefix_value,
    )
    text = text.replace("ё", "е").replace("Ё", "Е")
    result, reasons = {}, []
    for key, pattern in (("size", _SIZE_RE), ("fit", _FIT), ("color", _COLOR), ("garment_type", _GARMENT)):
        matches = list(pattern.finditer(text))
        values = set()
        for match in matches:
            token = match.group().casefold()
            if key == "size":
                value = _canonical_source_size(token)
            elif key == "fit":
                value = _find_prefix_value(match.group(), {**_FIT_WORDS, "отверсаз": "oversize"})
            elif key == "garment_type":
                value = _find_prefix_value(match.group(), {**_GARMENT_WORDS, "tee": "tshirt", "лонгслів": "longsleeve", "лонгслив": "longsleeve", "longsleeve": "longsleeve"})
            else:
                value = _find_prefix_value(match.group(), {**_COLOR_WORDS, "біл": "white"})
            if value:
                values.add(value)
        if len(values) == 1:
            result[key] = next(iter(values))
        elif values:
            reasons.append("ambiguous_" + key)
    quantity = re.findall(r"(?<!\d)(\d{1,2})\s*(?:шт\.?|штук\w*|одиниц\w*|items?|футболк\w*)\b", text, re.I)
    if len(set(quantity)) == 1 and 0 < int(quantity[0]) <= 20:
        result["qty"] = int(quantity[0])
    elif quantity:
        reasons.append("ambiguous_quantity")
    return result, reasons


def _title(text):
    labelled = _MODEL_LABEL.search(text)
    # A brand or explicit garment anchors an offsite name. Never reinterpret a
    # model number as a catalogue primary key or an amount without currency.
    anchor = _BRAND.search(text) or _GARMENT.search(text)
    if not anchor and not labelled:
        return ""
    raw = labelled.group(1) if labelled else text[anchor.start():]
    raw = _AMOUNT.sub(" ", raw)
    for pattern in (_SIZE, _FIT, _COLOR):
        raw = pattern.sub(" ", raw)
    raw = re.sub(r"\b(?:розмір|размер|size|колір|цвет|color|крій|крой|fit|беру|замовляю|заказываю)\b", " ", raw, flags=re.I)
    raw = re.split(r"[\n;]|\b(?:ціна|цена|вартість|стоимость|до\s+сплати|оплата|доставка)\b", raw, maxsplit=1, flags=re.I)[0]
    return " ".join(raw.split()).strip(" .,!:=-")[:255]


def _offer_item(row, references):
    # A trailing seller confirmation question is not a question about any
    # selection axis. Ambiguous alternatives and other questions still abstain.
    text = re.sub(r"[\s.,;]+(?:все\s+вірно|усе\s+вірно|все\s+правильно|всё\s+верно|все\s+верно|"
        r"правильно|вірно|is\s+that\s+correct|is\s+everything\s+correct|correct)\s*\?\s*$", "", row["text"], flags=re.I)
    config, reasons = _configuration(text)
    title = _title(text)
    if reasons or not config or not (title or config.get("garment_type")):
        return None, reasons
    # A seller's conversational remark about a garment is not a configuration
    # offer. Require at least a size/fit/colour or an explicit model label.
    if not set(config) & {"size", "fit", "color"} and not _MODEL_LABEL.search(text):
        return None, []
    color_labels = {"white": "Білий", "black": "Чорний", "grey": "Сірий", "blue": "Синій", "green": "Зелений", "pink": "Рожевий"}
    model_name = _BRAND.sub(" ", _GARMENT.sub(" ", title)).strip(" .,:;-1234567890")
    named = bool(_MODEL_LABEL.search(text) or len(model_name) >= 3 or re.search(r"\d{2,}", title))
    return {"product_id": None, "title": title or "Товар поза каталогом", "size": config.get("size", ""),
        "fit": config.get("fit", ""), "fit_option_code": config.get("fit", ""),
        "color": config.get("color", ""), "color_name": color_labels.get(config.get("color"), ""),
        "garment_type": config.get("garment_type", ""), "qty": config.get("qty"), "unit_price": None,
        "source_message_id": row["id"], "acceptance_message_id": None,
        "price_evidence_message_ids": [], "reference_message_ids": list(references),
        "identity_kind": "offsite_named" if named else "unresolved_garment", "identity_status": "seller_source",
        "authority": "seller_offer", "configuration_authority": "seller_offer"}, []


def _accepts(row, item):
    text = row["text"].strip(" .,!🙂👍🙏✅")
    if not _ACCEPT.search(text) or "?" in text or _NEGATIVE.search(text) or _RESET.search(text) or re.search(r"[«»“”\"]", text):
        return False
    # A different amount or an objection is a counteroffer, not acceptance.
    if re.search(r"\b(?:але|но|but|тільки|только|instead|якщо|если|if)\b", text, re.I) or _AMOUNT.search(text):
        return False
    config, reasons = _configuration(text)
    if reasons:
        return False
    if item and any(item.get(key if key != "fit" else "fit_option_code") not in (None, "", value)
                    for key, value in config.items()):
        return False
    # Confirmation must stand alone or restate the configuration, not report
    # somebody else's answer or answer an unrelated question.
    tail = _ACCEPT.sub("", text, count=1).strip(" ,.!:")
    if not tail or re.fullmatch(r"(?:дякую|спасибо|thanks|thank you)", tail, re.I):
        return True
    repeated = _ACCEPT.match(tail)
    if repeated and not tail[repeated.end():].strip(" ,.!:"):
        return True
    return bool(config) and not re.search(r"\b(?:сказ\w*|напис\w*|питан\w*|вопрос|said|wrote)\b", tail, re.I)


def _counteroffer(row):
    text = row["text"]
    return bool(_AMOUNT.search(text)
        and not re.search(r"оплат\w*|сплат\w*|переказ\w*|передоплат\w*|receipt|paid|deposit", text, re.I)
        and re.search(r"\b(?:за|ціна|цена|price|вартість|стоимость|тільки|только|only|instead|пропоную|предлагаю)\b", text, re.I))


def _source_clauses(text):
    """Keep question/negation scope local while preserving source offsets."""
    start = 0
    for boundary in re.finditer(r"[,;.!?\n]|\b(?:але|но|but)\b", text, re.I):
        end = boundary.end() if boundary.group() == "?" else boundary.start()
        yield start, end, text[start:end]
        start = boundary.end()
    yield start, len(text), text[start:]


def _customer_garment_requirement(row):
    """An affirmative purchase clause can precede unrelated objections.

    Singular grammar supplies quantity only for one garment mention in the
    entire source. Alternatives, multiple garments and reported requests
    deliberately require completion instead of a default quantity.
    """
    from management.services.ig_commerce_turns import _is_quoted_preference

    text = row["text"]
    mentions = list(_GARMENT.finditer(text))
    if (not mentions or re.search(r"\b(?:сказ\w*|напис\w*|said|wrote)\b", text, re.I)
            or (len(mentions) > 1 and re.search(r"\b(?:або|или|чи|or)\b", text[mentions[0].end():mentions[-1].start()], re.I))):
        return {}
    requirements = []
    for start, _end, clause in _source_clauses(text):
        intent = re.search(r"\b(?:хочу|беру|замовляю|заказываю|купую|want|order)\b[^.!?]{0,80}" + _GARMENT.pattern, clause, re.I)
        if not intent or _is_quoted_preference(text, start + intent.start(), start + intent.end()):
            continue
        configuration, reasons = _configuration(clause)
        if reasons or not configuration.get("garment_type"):
            continue
        requirement = {"garment_type": configuration["garment_type"], "garment_source_message_id": row["id"]}
        if len(mentions) == 1:
            if configuration.get("qty"):
                requirement.update(qty=configuration["qty"], quantity_source_message_id=row["id"], quantity_inference="explicit_quantity")
            elif re.search(r"\bфутболку\b|\ba\s+t-?shirt\b|\bone\s+(?:t-?shirt|hoodie)\b", clause, re.I):
                requirement.update(qty=1, quantity_source_message_id=row["id"], quantity_inference="singular_garment")
        requirements.append(requirement)
    return requirements[0] if len(requirements) == 1 else {}


def _withdraws_agreement(row):
    text = row["text"]
    return bool(not _packaging(row).get("exclude_receipt") and any(_NEGATIVE.search(clause) and re.search(
        r"замов\w*|заказ\w*|беру|куп\w*|принт\w*|модел\w*|cancel|order|size|розмір|размер|"
        + _FIT.pattern + "|" + _COLOR.pattern + "|" + _GARMENT.pattern, clause, re.I)
        for _start, _end, clause in _source_clauses(text)))


def _quoted_amounts(row):
    from management.services.ig_commerce_turns import _is_quoted_preference

    text = row["text"]
    first_amount = _AMOUNT.search(text)
    if first_amount and re.search(r"(?:раніше|раньше|previously)[^.!?]{0,30}(?:пис\w*|напис\w*|said|wrote)|"
            r"(?:він|вона|он|она|they|he|she)\s+(?:пис\w*|напис\w*|сказ\w*|said|wrote)", text[:first_amount.start()], re.I):
        return {}, ["amount_not_affirmed"]
    split = _SPLIT.search(text) if re.search(_DELIVERY, text, re.I) else None
    if split and not ("?" in text or _NEGATIVE.search(text) or _is_quoted_preference(text, split.start(), split.end())):
        if len({_currency(match.group("currency")) for match in _AMOUNT.finditer(split.group())}) != 1:
            return {}, ["amount_currency_conflict"]
        values = [_money(split.group(key), allow_zero=key == "delivery") for key in ("merch", "delivery", "total")]
        if not all(values) or Decimal(values[0]) + Decimal(values[1]) != Decimal(values[2]):
            return {}, ["amount_arithmetic_mismatch"]
        return {"merchandise_total": values[0], "delivery_amount": values[1], "payable_total": values[2],
            "currency": _currency(split.group("currency")), "authority": "seller_instruction", "source_message_id": row["id"],
            "acceptance_message_id": None, "arithmetic_verified": True}, []
    clauses = list(_amount_clauses(text))
    conditional = _conditional_shipping_rule(row)
    threshold_span = conditional[1] if conditional else None
    matches = [(match, clause) for match in _AMOUNT.finditer(text)
        for start, end, clause in clauses if start <= match.start() < end and "?" not in clause and not _NEGATIVE.search(clause)
        and not _is_quoted_preference(text, match.start(), match.end())
        and not (threshold_span and threshold_span[0] <= match.start() < threshold_span[1])]
    if not matches:
        return {}, []
    if len({_currency(match.group("currency")) for match, _clause in matches}) != 1:
        return {}, ["amount_currency_conflict"]
    values = {"currency": _currency(matches[0][0].group("currency")), "authority": "seller_instruction",
        "source_message_id": row["id"], "acceptance_message_id": None}
    reasons = []
    for match, clause in matches:
        price = bool(re.search(r"\b(?:ціна|цена|вартість|стоимость|за|price|cost)\b", clause, re.I) or _GARMENT.search(clause))
        payable = bool(re.search(r"\b(?:сума|сумма|разом|итого|всього|всего|total|iban|оплат\w*|сплат\w*)\b|UA\d{27}", clause, re.I))
        delivery = bool(re.search(_DELIVERY, clause, re.I))
        deposit = bool(re.search(r"передоплат\w*|аванс\w*|deposit|prepay", clause, re.I))
        if deposit:
            key = "requested_payment_amount"
        elif payable:
            key = "payable_total"
            if delivery:
                reasons.append("delivery_allocation_required")
        elif delivery and not _GARMENT.search(clause):
            key = "delivery_amount"
        elif price:
            key = "merchandise_total"
        else:
            return {}, ["amount_purpose_unknown"]
        amount = _money(match.group("value"), allow_zero=key == "delivery_amount")
        if not amount:
            return {}, ["amount_invalid"]
        if key in values:
            return {}, ["amount_allocation_required"]
        values[key] = amount
    if values.get("merchandise_total") and "delivery_amount" in values:
        total = str((Decimal(values["merchandise_total"]) + Decimal(values["delivery_amount"])).quantize(Decimal("0.01")))
        if values.get("payable_total", total) != total:
            return {}, ["amount_arithmetic_mismatch"]
        values.update(payable_total=total, arithmetic_verified=True)
        reasons = []
    return values, reasons


def _amount_clauses(text):
    """Clause scope retaining decimals and trailing question delimiters."""
    start = 0
    for boundary in re.finditer(r"(?<!\d)[,.]|[,.;](?!\d)|[!?\n]|\b(?:але|но|but)\b", text, re.I):
        end = boundary.end() if boundary.group() == "?" else boundary.start()
        yield start, end, text[start:end]
        start = boundary.end()
    yield start, len(text), text[start:]


def _unknown_shipping_payment():
    return {"mode": "unknown", "customer_charge_amount": "", "included_in_payable_total": None,
        "payer_type": "", "source_message_id": None, "evidence_message_ids": [],
        "customer_request_message_id": None, "authority": "seller_instruction"}


def _conditional_shipping_rule(row):
    """Extract a bounded monetary condition, never a quoted delivery fee."""
    text = row["text"]
    if not re.search(_DELIVERY, text, re.I) or not re.search(_MERCHANT_SHIPPING, text, re.I):
        return None
    number = r"(?:\d{1,3}(?:[ \u00a0]\d{3})+|" + _NUMBER + r")"
    prefix = r"(?:>=|>|від|от|from|понад|свыше|вище|выше|over|above|більше|больше|более|more\s+than|at\s+least)"
    pattern = re.compile(r"(?<!\w)(?P<operator>" + prefix + r")\s*(?P<amount>" + number
        + r")\s*(?P<currency>" + _CURRENCY + r")?(?!\w)", re.I)
    matches = list(pattern.finditer(text))
    suffix = list(re.finditer(r"(?<!\d)(?P<amount>" + number + r")\s*(?P<currency>" + _CURRENCY
        + r")\s*(?:or\s+more|і\s+більше|и\s+более)(?!\w)", text, re.I))
    for match in suffix:
        if not any(start.start() <= match.start() < start.end() for start in matches):
            matches.append(match)
    if not matches:
        # A conditional promise with an unsupported condition abstains rather
        # than turning the words "free delivery" into unconditional coverage.
        if re.search(r"\b(?:якщо|если|if|при\s+(?:замов\w*|заказ\w*)|(?:for|on)\s+orders|для\s+(?:замов\w*|заказ\w*))\b", text, re.I):
            return ({"kind": "merchandise_total_threshold", "operator": "", "threshold_amount": "",
                "currency": "", "source_message_id": row["id"]}, (re.search(_DELIVERY, text, re.I).start(), len(text)))
        return None
    if len(matches) != 1:
        return ({"kind": "merchandise_total_threshold", "operator": "", "threshold_amount": "",
            "currency": "", "source_message_id": row["id"]}, (0, len(text)))
    match = matches[0]
    operator = " ".join((match.groupdict().get("operator") or "or more").casefold().split())
    strict = operator in {">", "понад", "свыше", "вище", "выше", "over", "above", "більше", "больше", "более", "more than"}
    value = _money(re.sub(r"[ \u00a0]", "", match.group("amount")))
    return ({"kind": "merchandise_total_threshold", "operator": ">" if strict else ">=",
        "threshold_amount": value, "currency": _currency(match.group("currency")) if match.group("currency") else "",
        "source_message_id": row["id"]}, (match.start(), match.end()))


def _evaluate_shipping_condition(payment, amount):
    value = deepcopy(payment)
    condition = deepcopy(value["condition"])
    merchandise = amount.get("merchandise_total") or ""
    currency = amount.get("currency") or ""
    if not condition.get("operator") or not condition.get("threshold_amount") or not condition.get("currency"):
        status, reason = "unknown", "shipping_condition_unknown"
    elif not merchandise:
        status, reason = "unknown", "shipping_condition_amount_unknown"
    elif currency != condition["currency"]:
        status, reason = "currency_mismatch", "shipping_condition_currency_mismatch"
    else:
        met = (Decimal(merchandise) > Decimal(condition["threshold_amount"]) if condition["operator"] == ">"
            else Decimal(merchandise) >= Decimal(condition["threshold_amount"]))
        status, reason = ("met", "") if met else ("not_met", "shipping_condition_not_met")
    condition.update(status=status, evaluated_merchandise_total=merchandise, evaluated_currency=currency,
        amount_source_message_id=amount.get("source_message_id"))
    value.update(condition=condition, mode="merchant_free" if status == "met" else "unknown",
        customer_charge_amount="0.00" if status == "met" else "", included_in_payable_total=False if status == "met" else None,
        payer_type="Sender" if status == "met" else "")
    ids = [condition["source_message_id"], amount.get("source_message_id"), value.get("customer_request_message_id")]
    value["evidence_message_ids"] = sorted({source_id for source_id in ids if isinstance(source_id, int)})
    return value, [reason] if reason else []


def _shipping_payment(row, amount):
    """A seller arrangement, never evidence of money received or carrier paid."""
    modes = set()
    unaffirmed = False
    conditional = _conditional_shipping_rule(row)
    delivery_anchor = re.search(_DELIVERY, row["text"], re.I)
    if delivery_anchor and re.search(r"(?:раніше|раньше|previously)[^.!?]{0,30}(?:пис\w*|напис\w*|said|wrote)|"
            r"(?:він|вона|он|она|they|he|she)\s+(?:пис\w*|напис\w*|сказ\w*|said|wrote)", row["text"][:delivery_anchor.start()], re.I):
        unknown = _unknown_shipping_payment()
        unknown.update(source_message_id=row["id"], evidence_message_ids=[row["id"]])
        return unknown, ["shipping_payment_not_affirmed"]
    for start, end, clause in _amount_clauses(row["text"]):
        delivery = re.search(_DELIVERY, clause, re.I)
        if not delivery:
            continue
        from management.services.ig_commerce_turns import _is_quoted_preference
        alternatives = re.sub(r"\bor\s+more\b", "", clause, flags=re.I) if conditional else clause
        if ("?" in clause or _NEGATIVE.search(clause) or re.search(r"\b(?:небезкоштов\w*|небесплат\w*|або|или|or)\b", alternatives, re.I)
                or _is_quoted_preference(row["text"], start + delivery.start(), start + delivery.end())):
            unaffirmed = True
            continue
        if re.search(_MERCHANT_SHIPPING, clause, re.I):
            modes.add("merchant_free")
        if re.search(r"(?:отримувач\w*|одержувач\w*|получател\w*|recipient|customer)\s+(?:оплачу\w*|оплачива\w*|сплачу\w*|плат\w*|pays?)|(?:оплачу\w*|оплачива\w*|сплачу\w*)\s+(?:отримувач\w*|одержувач\w*|получател\w*)|(?:paid|payable)\s+by\s+(?:recipient|customer)|(?:оплат\w*|сплат\w*|pay\w*)[^.!?]{0,20}(?:при\s+отриман\w*|при\s+получен\w*|на\s+пошті|на\s+почте|carrier|upon\s+receipt)", clause, re.I):
            modes.add("carrier_recipient")
    unknown = _unknown_shipping_payment()
    unknown.update(source_message_id=row["id"], evidence_message_ids=[row["id"]])
    if unaffirmed:
        return unknown, ["shipping_payment_not_affirmed"]
    if len(modes) > 1:
        return unknown, ["shipping_payment_conflict"]
    if conditional and modes == {"merchant_free"}:
        unknown["condition"] = conditional[0]
        return _evaluate_shipping_condition(unknown, amount)
    charge = amount.get("delivery_amount", "")
    if modes and charge and Decimal(charge) > 0:
        return unknown, ["shipping_payment_conflict"]
    mode = next(iter(modes)) if modes else ("merchant_free" if charge == "0.00" else "customer_prepaid" if charge and amount.get("payable_total") and amount.get("merchandise_total") else "unknown")
    if mode == "unknown":
        return None, []
    value = _unknown_shipping_payment()
    value.update(mode=mode, customer_charge_amount=charge if mode == "customer_prepaid" else "0.00",
        included_in_payable_total=mode == "customer_prepaid", payer_type="Recipient" if mode == "carrier_recipient" else "Sender",
        source_message_id=row["id"], evidence_message_ids=[row["id"]])
    return value, []


def _quote_expiry(text):
    label = re.search(r"(?:діє\s+до|действует\s+до|quote\s+expires(?:\s+at)?|valid\s+until)\s*[:=-]?\s*(\S+)", text, re.I)
    if not label:
        return "", ""
    try:
        stamp = datetime.fromisoformat(label.group(1).rstrip(".,;"))
    except ValueError:
        return "", "quote_expiry_unknown"
    if stamp.tzinfo is None:
        return "", "quote_expiry_timezone_unknown"
    return stamp.isoformat(), ""


def _instruction(row):
    text = row["text"]
    iban = re.search(r"(?<!\w)UA(?:\s*\d){27}(?!\d)", text, re.I)
    if not iban:
        return {}
    recipient = re.search(r"(?:отримувач|одержувач|получатель|recipient)\s*[:=-]\s*([^\n;]{3,150})", text, re.I)
    return {"iban": re.sub(r"\s+", "", iban.group()).upper(),
        "recipient": recipient.group(1).strip() if recipient else "",
        "currency": "UAH", "source_message_id": row["id"], "authority": "seller_instruction"}


def _shipping(row):
    text, result = row["text"], {}
    word = r"[A-Za-zА-Яа-яІіЇїЄєҐґЁё][A-Za-zА-Яа-яІіЇїЄєҐґЁё'’\-]*"
    blocked = (r"пош\w*|поч\w*|відділен\w*|отделен\w*|област\w*|район\w*|футбол\w*|"
        r"оплат\w*|подар\w*|так|добре|вітаю|привіт|здравствуйте|привет|hello|hi|thanks|дякую|спасибо|"
        r"добрий|добрый|доброго|доброе|день|ранок|вечір|хочу|want|please|будь|ласка|телефон|номер|phone|не|ні|нет|not|no|або|или|or")
    def address_phrase(value, maximum=6):
        return bool(value.casefold() not in {"місто", "город", "city"}
            and re.fullmatch(word + r"(?:\s+" + word + r"){0," + str(maximum - 1) + r"}", value)
            and not re.search(r"\b(?:" + blocked + r")\b", value, re.I))

    labels = {"full_name": r"ПІБ|ПИБ|ФІО|ФИО|отримувач|получатель|recipient", "city": r"місто|город|city",
        "office": r"відділен\w*|отделен\w*|поштомат|office"}
    for key, label in labels.items():
        match = re.search(r"(?:" + label + r")\s*[:№=-]\s*([^\n;/]{1,140})", text, re.I)
        if match:
            result[key] = match.group(1).strip()
    phone = re.search(r"(?<!\d)(?:\+?380|0)\d{9}(?!\d)", re.sub(r"[ ()-]", "", text))
    if phone:
        result["phone"] = phone.group()
    office = re.search(r"\b(відділен\w*|отделен\w*|поштомат|office|НП)\s*[:№=-]?\s*(\d{1,8})\b", text, re.I)
    if office:
        result["office"] = ("Відділення" if office.group(1).casefold() == "нп" else office.group(1)) + " №" + office.group(2)
    office_suffix = r"\b(?:НП|нова\s+пошта|новая\s+почта|відділен\w*|отделен\w*|поштомат|office)\s*[:№#=-]?\s*\d+\b"
    if result.get("city"):
        result["city"] = re.split(office_suffix, result["city"], maxsplit=1, flags=re.I)[0].strip(" .,:;")
        if not address_phrase(result["city"]):
            result.pop("city")
    city = re.search(r"(?:^|[\n/;,])\s*(?:м\.|г\.|(?:місто|город|city)\s*[:=-])\s*([^\n/;,]{2,100})", text, re.I)
    if city and "city" not in result:
        value = re.split(office_suffix, city.group(1), maxsplit=1, flags=re.I)[0].strip(" .,:;")
        if address_phrase(value):
            result["city"] = value
    comma_city = re.search(r"(?:^|[\n/;])\s*([^\n/;,\d]{2,100})\s*,\s*(?:НП|нова\s+пошта|новая\s+почта|відділен\w*|отделен\w*|поштомат)\b", text, re.I)
    if comma_city and "city" not in result and address_phrase(comma_city.group(1).strip()):
        result["city"] = comma_city.group(1).strip()
    # Unlabelled name/city lines are accepted only inside the customer's
    # structured contact message containing a phone, never from follow-ups.
    if phone:
        lines = [line.strip(" .,:;") for line in re.split(r"[\n/;]+", text) if line.strip()]
        lines = [re.sub(r"(?:\+?380|0)\d{9}(?!\d)", " ", line).strip(" .,:;") for line in lines]
        name_candidates = [line for line in lines if re.fullmatch(word + r"(?:\s+" + word + r"){1,3}", line)
            and address_phrase(line, maximum=4)]
        if len(name_candidates) == 1 and "full_name" not in result:
            result["full_name"] = name_candidates[0]
        city_candidates = [line for line in lines if re.fullmatch(word, line)
            and address_phrase(line)]
        if len(city_candidates) == 1 and "city" not in result:
            result["city"] = city_candidates[0]
        # A customer may omit the comma before the carrier marker. The phone
        # message and an explicit office suffix bind this prefix as address
        # data; greeting/ordinary chat and alternatives never become a city.
        inline_candidates = []
        for line in lines:
            suffix = re.search(office_suffix, line, re.I)
            if suffix and not line[suffix.end():].strip(" .,:;"):
                prefix = line[:suffix.start()].strip(" .,:;")
                prefix = re.sub(r"^(?:м\.|г\.|(?:місто|город|city)\s*[:=-])\s*", "", prefix, flags=re.I)
                if address_phrase(prefix):
                    inline_candidates.append(prefix)
        if len(set(inline_candidates)) == 1 and "city" not in result:
            result["city"] = inline_candidates[0]
    if result:
        result["field_evidence"] = {key: {"source_message_id": row["id"]} for key in result}
        result.update(source_message_id=row["id"], authority="customer_source")
    return result


def _packaging(row):
    text = row["text"]
    exclude = bool(re.search(r"(?:без|without|no)\s+(?:чек\w*|квитанц\w*|receipt|invoice)", text, re.I)
        or re.search(r"(?:не|don't|do\s+not)\s+(?:вклад\w*|клад\w*|кладіть|дода\w*|приклад\w*|put|include)[^.!?]{0,50}(?:чек\w*|квитанц\w*|receipt|invoice)", text, re.I)
        or re.search(r"(?:чек\w*|квитанц\w*|receipt|invoice)[^.!?]{0,20}(?:не|not|don't)[^.!?]{0,16}(?:вклад\w*|класти|дода\w*|put|include)", text, re.I))
    gift = bool(re.search(r"подар\w*|gift", text, re.I))
    if not exclude and not gift:
        return {}
    if "?" in text:
        return {}
    return {"exclude_receipt": exclude, "gift": gift, "source_message_id": row["id"], "authority": "customer_source"}


def _media_parts(row):
    media = row.get("media") or row.get("attachment_media")
    if not isinstance(media, list) or not media:
        try:
            media = json.loads(row.get("attachments") or "[]")
        except (TypeError, ValueError):
            media = []
    return media if isinstance(media, list) else []


def _reference(row):
    media = _media_parts(row)
    if re.search(r"оплат\w*|сплат\w*|квитанц\w*|receipt|paid", row["text"], re.I):
        return False
    return isinstance(media, list) and any(isinstance(item, dict)
        and item.get("role") not in {"receipt", "payment_candidate"}
        and item.get("payment_evidence") is not True
        and (item.get("role") in {"product", "custom_reference", "reference", "screenshot"}
             or any(str(item.get(key) or "").casefold() in {"image", "photo", "screenshot", "ig_post", "share"}
                    for key in ("type", "media_type"))
             or str(item.get("mime") or "").casefold().startswith("image/")) for item in media)


def extract_conversation_agreement(messages):
    """Return source wishes and typed quotes; never infer paid/catalogue truth."""
    raw = list(messages or ())
    reasons = ["transcript_truncated"] if len(raw) > MAX_MESSAGES else []
    rows = [_row(row) for row in raw[-MAX_MESSAGES:]]
    result = {"schema": SCHEMA, "items": [], "amounts": {}, "payment_instruction": {}, "shipping": {}, "packaging": {},
        "shipping_payment": _unknown_shipping_payment(),
        "source_message_ids": [], "evidence_message_ids": [], "evidence": {}, "uncertainty_reasons": reasons,
        "watermark_message_id": max((row["id"] for row in rows), default=0)}
    if any(not row["id"] for row in rows) or len({row["id"] for row in rows}) != len(rows) or any(a["id"] >= b["id"] for a, b in zip(rows, rows[1:])):
        reasons.append("transcript_identity_or_order_invalid")
        rows = []
    pending, pending_at, references = None, None, []
    pending_money, money_at = {}, None
    customer_requirement = {}
    customer_shipping_request_id = None
    additive_pending = False
    additive_source_id = None
    proofs = {}
    for index, row in enumerate(rows):
        if row.get("status") == "failed":
            continue
        customer = row["role"] in _CUSTOMERS
        additive = bool(customer and result["items"] and _GARMENT.search(row["text"])
            and re.search(r"\b(?:дода[йт]\w*|добав\w*|add)\b[^.!?]{0,60}\b(?:ще|ещ[её]|another|more|additional)\b", row["text"], re.I)
            and "?" not in row["text"] and not _NEGATIVE.search(row["text"]))
        if additive:
            additive_pending = True
            additive_source_id = row["id"]
            proofs[row["id"]] = _proof(row)
            reasons.append("additional_item_pending")
        if customer and _RESET.search(row["text"]) and not additive:
            pending, pending_money, references, customer_requirement = None, {}, [], {}
            additive_pending = False
            result.update(items=[], amounts={}, payment_instruction={}, shipping={}, packaging={}, shipping_payment=_unknown_shipping_payment())
            customer_shipping_request_id = None
            proofs = {}
            reasons.append("customer_selection_reset")
            continue
        if customer and _counteroffer(row):
            pending, pending_money = None, {}
            additive_pending = False
            result.update(items=[], amounts={}, shipping_payment=_unknown_shipping_payment())
            customer_shipping_request_id = None
            reasons.append("customer_counteroffer_pending")
            continue
        requirement = _customer_garment_requirement(row) if customer else {}
        if customer and _NEGATIVE.search(row["text"]) and not _packaging(row).get("exclude_receipt") and not requirement:
            pending, pending_money = None, {}
            additive_pending = False
            # A direct withdrawal invalidates the current agreement. It must
            # not revive an older offer on the next short "yes".
            if _withdraws_agreement(row):
                customer_requirement = {}
                if result["items"]:
                    result["items"] = []
                    result["amounts"] = {}
                    result["shipping_payment"] = _unknown_shipping_payment()
                    customer_shipping_request_id = None
                    reasons.append("customer_configuration_withdrawn")
            continue
        if customer:
            if (re.search(_DELIVERY, row["text"], re.I)
                    and re.search(r"пораху\w*|розраху\w*|посчита\w*|рассчита\w*|calculat\w*|include\w*", row["text"], re.I)
                    and not _NEGATIVE.search(row["text"])
                    and not re.search(r"[«»“”\"]", row["text"])):
                customer_shipping_request_id = row["id"]
                result["shipping_payment"]["customer_request_message_id"] = row["id"]
                result["shipping_payment"]["evidence_message_ids"] = sorted(set([
                    *result["shipping_payment"]["evidence_message_ids"], row["id"]]))
                proofs[row["id"]] = _proof(row)
            if requirement:
                customer_requirement = requirement
                proofs[row["id"]] = _proof(row)
            fresh_reference_offer = (pending is not None and pending_at is not None
                and index - pending_at <= 3 and pending.get("acceptance_message_id") is None)
            if _reference(row) and (not result["items"] or fresh_reference_offer):
                references.append(row["id"])
                references = references[-8:]
                proofs[row["id"]] = _proof(row)
                if pending is not None:
                    pending["reference_message_ids"] = list(references)
            shipping, packaging = _shipping(row), _packaging(row)
            if shipping:
                field_evidence = {**result["shipping"].get("field_evidence", {}), **shipping.get("field_evidence", {})}
                result["shipping"].update(shipping)
                result["shipping"]["field_evidence"] = field_evidence
                proofs[row["id"]] = _proof(row)
            if packaging:
                result["packaging"].update(packaging)
                proofs[row["id"]] = _proof(row)
            fresh = pending_at is not None and index - pending_at <= 3
            accepts = _accepts(row, pending)
            if pending and fresh and accepts:
                pending.update(acceptance_message_id=row["id"], authority="conversation_agreement", configuration_authority="customer_confirmed_seller_offer")
                pending["identity_status"] = "customer_confirmed_source"
                if additive_pending:
                    pending["addition_source_message_id"] = additive_source_id
                result["items"] = [*result["items"], deepcopy(pending)] if additive_pending else [deepcopy(pending)]
                additive_pending = False
                reasons[:] = [reason for reason in reasons if reason != "additional_item_pending"]
                proofs[row["id"]] = _proof(row)
                # Later captionless receipt/media cannot silently alter the
                # print the customer already confirmed. A new design needs a
                # new explicit offer or a selection reset.
                pending, pending_at, references = None, None, []
            if pending_money and money_at is not None and index - money_at <= 3 and accepts:
                pending_money.update(acceptance_message_id=row["id"], authority="conversation_agreement")
                result["amounts"] = deepcopy(pending_money)
                proofs[row["id"]] = _proof(row)
            # Any substantive question/counteroffer ends the confirmation
            # anchor; a later answer cannot accidentally affirm an older one.
            if not accepts and row["text"].strip() and not shipping and not packaging:
                pending = None
                pending_money = {}
            continue
        if not _seller(row):
            continue
        item, item_reasons = _offer_item(row, references)
        if item:
            if customer_requirement and item["garment_type"] in ("", customer_requirement["garment_type"]):
                if not item["garment_type"]:
                    item.update(garment_type=customer_requirement["garment_type"],
                        garment_source_message_id=customer_requirement["garment_source_message_id"])
                if item["qty"] is None and customer_requirement.get("qty"):
                    item.update(qty=customer_requirement["qty"], quantity_source_message_id=customer_requirement["quantity_source_message_id"],
                        quantity_inference=customer_requirement["quantity_inference"])
            pending, pending_at = item, index
            proofs[row["id"]] = _proof(row)
        elif item_reasons and (_GARMENT.search(row["text"]) or _BRAND.search(row["text"])):
            pending = None
            reasons.extend(item_reasons)
        if (pending is not None and pending_at is not None and index - pending_at <= 3
                and pending.get("acceptance_message_id") is None and _reference(row)):
            references = list(dict.fromkeys([*(pending.get("reference_message_ids") or []), row["id"]]))[-8:]
            pending["reference_message_ids"] = list(references)
            pending["accepted_reference_message_ids"] = list(dict.fromkeys([
                *(pending.get("accepted_reference_message_ids") or []), row["id"]]))[-8:]
            proofs[row["id"]] = _proof(row)
        amount, amount_reasons = _quoted_amounts(row)
        if amount:
            prior_amount = result["amounts"]
            if ("delivery_amount" in amount and not amount.get("merchandise_total") and not amount.get("payable_total")
                    and prior_amount.get("merchandise_total") and amount["currency"] == prior_amount.get("currency")):
                amount.update(merchandise_total=prior_amount["merchandise_total"],
                    merchandise_source_message_id=prior_amount.get("merchandise_source_message_id") or prior_amount["source_message_id"],
                    payable_total=str((Decimal(prior_amount["merchandise_total"]) + Decimal(amount["delivery_amount"])).quantize(Decimal("0.01"))),
                    arithmetic_verified=True,
                    evidence_message_ids=sorted(set([*(prior_amount.get("evidence_message_ids") or []), prior_amount["source_message_id"], row["id"]])))
            expires_at, expiry_reason = _quote_expiry(row["text"])
            if expires_at:
                amount["expires_at"] = expires_at
            if expiry_reason:
                reasons.append(expiry_reason)
            pending_money, money_at = amount, index
            result["amounts"] = deepcopy(amount)
            proofs[row["id"]] = _proof(row)
        if amount_reasons:
            pending_money = {}
            reasons.extend(amount_reasons)
            if "amount_arithmetic_mismatch" in amount_reasons:
                result["amounts"] = {}
                result["shipping_payment"] = _unknown_shipping_payment()
        shipping_payment, shipping_reasons = _shipping_payment(row, amount)
        if shipping_payment is not None and shipping_payment.get("condition"):
            shipping_payment, shipping_reasons = _evaluate_shipping_condition(shipping_payment, result["amounts"])
        elif shipping_payment is None and result["shipping_payment"].get("condition") and (amount or amount_reasons):
            shipping_payment, shipping_reasons = _evaluate_shipping_condition(result["shipping_payment"], result["amounts"])
        if shipping_payment is not None:
            shipping_payment["customer_request_message_id"] = customer_shipping_request_id
            if customer_shipping_request_id:
                shipping_payment["evidence_message_ids"] = sorted(set([
                    *shipping_payment["evidence_message_ids"], customer_shipping_request_id]))
            result["shipping_payment"] = shipping_payment
            proofs[row["id"]] = _proof(row)
            # The free/carrier arrangement supplies a zero customer charge;
            # it does not alter the value of the merchandise or claim payment.
            if shipping_payment["mode"] in {"merchant_free", "carrier_recipient"}:
                current_amounts = result["amounts"]
                merchandise = current_amounts.get("merchandise_total")
                if not merchandise and "delivery_allocation_required" in amount_reasons:
                    merchandise = current_amounts.get("payable_total")
                if merchandise:
                    current_amounts.update(merchandise_total=merchandise, delivery_amount="0.00", payable_total=merchandise,
                        delivery_source_message_id=shipping_payment["source_message_id"])
                    pending_money = deepcopy(current_amounts)
            elif shipping_payment.get("condition") and result["amounts"].get("delivery_source_message_id"):
                current_amounts = result["amounts"]
                current_amounts.pop("delivery_amount", None)
                current_amounts.pop("delivery_source_message_id", None)
                if current_amounts.get("payable_total") == current_amounts.get("merchandise_total"):
                    current_amounts.pop("payable_total", None)
                pending_money = deepcopy(current_amounts)
            if shipping_payment.get("condition"):
                reasons[:] = [reason for reason in reasons if not reason.startswith("shipping_condition_")]
            if shipping_payment["mode"] != "unknown":
                reasons[:] = [reason for reason in reasons if reason not in {"delivery_allocation_required", "shipping_payment_conflict", "shipping_payment_not_affirmed"}]
        elif amount.get("payable_total") and "delivery_allocation_required" in amount_reasons:
            result["shipping_payment"] = _unknown_shipping_payment()
            result["shipping_payment"].update(source_message_id=row["id"], customer_request_message_id=customer_shipping_request_id,
                evidence_message_ids=sorted({row["id"]} | ({customer_shipping_request_id} if customer_shipping_request_id else set())))
        reasons.extend(shipping_reasons)
        instruction = _instruction(row)
        if instruction:
            result["payment_instruction"] = instruction
            proofs[row["id"]] = _proof(row)
    amounts = result["amounts"]
    if result["items"]:
        reasons.append("catalog_product_not_identified")
        for item in result["items"]:
            if item["qty"] is None:
                reasons.append("quantity_not_explicit")
            if len(result["items"]) > 1:
                item["unit_price"] = None
                item["price_evidence_message_ids"] = []
                item.pop("price_authority", None)
                reasons.append("multi_item_price_allocation_required")
            elif amounts.get("merchandise_total") and item["qty"] == 1:
                item["unit_price"] = amounts["merchandise_total"]
                item["price_evidence_message_ids"] = amounts.get("evidence_message_ids") or [amounts["source_message_id"]]
                item["price_authority"] = amounts["authority"]
            elif amounts.get("merchandise_total"):
                reasons.append("conversation_price_allocation_required")
            if item["reference_message_ids"]:
                reasons.append("reference_identity_unverified")
    result["source_message_ids"] = sorted(proofs)
    result["evidence_message_ids"] = sorted(proofs)
    result["evidence"] = {str(key): value for key, value in proofs.items()}
    result["uncertainty_reasons"] = list(dict.fromkeys(reasons))
    for key in ("merchandise_total", "delivery_amount", "payable_total", "currency"):
        result[key] = amounts.get(key, "")
    result["delivery_total"] = result["delivery_amount"]
    result["quoted_total"] = result["merchandise_total"]
    result["amount_source_message_id"] = amounts.get("source_message_id")
    result["payment_context"] = deepcopy(result["payment_instruction"])
    result["transcript_message_ids"] = [row["id"] for row in rows]
    return result


def _retained_material_rows(agreement, rows):
    """Compact old context only when its material facts reproduce exactly."""
    ids = set()
    for item in agreement.get("items") or []:
        ids.update(item.get(key) for key in ("source_message_id", "acceptance_message_id", "garment_source_message_id", "quantity_source_message_id", "addition_source_message_id"))
        ids.update(item.get("reference_message_ids") or [])
        ids.update(item.get("price_evidence_message_ids") or [])
    for key in ("amounts", "payment_instruction", "shipping", "packaging", "shipping_payment"):
        component = agreement.get(key) or {}
        ids.update(component.get(field) for field in ("source_message_id", "acceptance_message_id"))
        for proof in (component.get("field_evidence") or {}).values():
            ids.add(proof.get("source_message_id"))
        ids.update(component.get("evidence_message_ids") or [])
        condition = component.get("condition") or {}
        ids.update(condition.get(field) for field in ("source_message_id", "amount_source_message_id"))
    # Keep terminating/counteroffer/invalid-quote evidence and recent spacing;
    # removing either could make an old pending offer look freshly accepted.
    ids.update(row["id"] for row in rows if (row["role"] in _CUSTOMERS and (_NEGATIVE.search(row["text"]) or _RESET.search(row["text"]) or _counteroffer(row)))
        or (_seller(row) and (_quoted_amounts(row)[1] or _quote_expiry(row["text"])[1])))
    ids.update(row["id"] for row in rows[-3:])
    retained = [row for row in rows if row["id"] in ids]
    reproduced = extract_conversation_agreement(retained)
    critical = ("items", "amounts", "payment_instruction", "shipping", "packaging", "shipping_payment")
    if all(reproduced.get(key) == agreement.get(key) for key in critical):
        return retained
    return rows


def persist_conversation_agreement(client, messages, watermark):
    """Persist only reverified owned sources; no send/provider/payment effects."""
    from django.db import transaction
    from management.models import IgClient, IgCommerceSelectionSession, InstagramBotMessage
    from management.services.ig_conversation_routes import conversation_route_reset_floor
    from management.services.ig_memory_producer import _namespaces, _source_allowed
    from management.services.ig_commerce_projection import source_preferences_for

    rows = [_row(row) for row in list(messages or ())[-MAX_MESSAGES:]]
    try:
        watermark = int(watermark)
    except (ValueError, TypeError):
        return {"persisted": False, "reason": "agreement_watermark_invalid"}
    with transaction.atomic():
        locked = IgClient.objects.select_for_update().filter(pk=client.pk).first()
        if locked is None or locked.privacy_erasure_started_at is not None:
            return {"persisted": False, "reason": "agreement_client_unavailable"}
        floor = conversation_route_reset_floor(locked.pk)
        episode_floor = int(getattr(locked.current_commercial_episode, "opened_watermark_message_id", 0) or 0)
        floor_for_sources = max(floor, episode_floor)
        rows = [row for row in rows if floor_for_sources <= row["id"] <= watermark]
        ids = [row["id"] for row in rows]
        persisted = {row.pk: row for row in InstagramBotMessage.objects.filter(client_id=locked.pk, pk__in=ids)}
        if len(persisted) != len(ids):
            return {"persisted": False, "reason": "agreement_source_unavailable"}
        namespaces = _namespaces(persisted.values())
        verified = []
        namespace = None
        for row in rows:
            source = persisted[row["id"]]
            source_namespace = namespaces[source.pk]
            if (source.text != row["text"] or source.role.casefold() != row["role"] or source.sender_id != locked.igsid
                    or not source_namespace or not _source_allowed(source)
                    or (row.get("provider_namespace") and row["provider_namespace"] != source_namespace)):
                return {"persisted": False, "reason": "agreement_source_changed"}
            if namespace is not None and source_namespace != namespace:
                return {"persisted": False, "reason": "agreement_source_namespace_mismatch"}
            namespace = source_namespace
            verified.append({**_row(source), "provider_namespace": namespace})
        if not verified or not namespace:
            return {"persisted": False, "reason": "agreement_source_unavailable"}
        context = dict(locked.sales_context or {}) if isinstance(locked.sales_context, dict) else {}
        existing = context.get("conversation_agreement") or {}
        scope = {"client_id": locked.pk, "episode_id": locked.current_commercial_episode_id,
            "reset_floor": floor, "source_namespace": namespace or ""}
        if isinstance(existing, dict) and int(existing.get("watermark_message_id") or 0) > watermark and existing.get("scope") == scope:
            return {"persisted": False, "reason": "agreement_source_stale"}
        terminated = any(row["role"] in _CUSTOMERS and (_RESET.search(row["text"]) or _counteroffer(row) or _withdraws_agreement(row)) for row in verified)
        if isinstance(existing, dict) and existing.get("scope") == scope and not terminated:
            previous = _read_conversation_agreement(locked, episode_id=scope["episode_id"],
                source_namespace=scope["source_namespace"], reset_floor=floor, watermark=existing.get("watermark_message_id"),
                _allow_expired_quote_for_retention=True)
            if previous["reason"]:
                return {"persisted": False, "reason": "agreement_retained_" + previous["reason"], "requires_human_review": True}
            retained = _retained_material_rows(previous["agreement"], previous["source_rows"])
            joined = {row["id"]: row for row in retained}
            joined.update({row["id"]: row for row in verified})
            if len(joined) > MAX_MESSAGES:
                return {"persisted": False, "reason": "agreement_history_reconstruction_required", "requires_human_review": True}
            verified = [joined[key] for key in sorted(joined)]
        agreement = extract_conversation_agreement(verified)
        agreement["scope"] = scope
        agreement["watermark_message_id"] = watermark
        canonical = source_preferences_for(locked, episode_id=locked.current_commercial_episode_id)
        canonical_size_owned = "size" in (canonical.get("values") or {}) or "size" in (canonical.get("cleared") or {})
        if not canonical_size_owned:
            # Invalid correction evidence grants no fact authority. Still fail
            # closed at this mirror boundary rather than revive an old size.
            canonical_size_owned = IgCommerceSelectionSession.objects.filter(client_id=locked.pk,
                open_slot=1, state=IgCommerceSelectionSession.State.OPEN,
                commercial_episode_id=locked.current_commercial_episode_id,
                transitions__action="manager_size_correction").exists()
        context["conversation_agreement"] = agreement
        locked.sales_context = context
        fields = ["sales_context", "updated_at"]
        if len(agreement["items"]) == 1 and locked.current_product_id is None:
            item = agreement["items"][0]
            for field, key in (("current_size", "size"), ("current_color", "color"), ("current_qty", "qty")):
                if field == "current_size" and canonical_size_owned:
                    continue
                if item.get(key) not in (None, ""):
                    setattr(locked, field, item[key])
                    fields.append(field)
        elif (not agreement["items"] and locked.current_product_id is None
                and any(reason in agreement["uncertainty_reasons"] for reason in ("customer_selection_reset", "customer_configuration_withdrawn"))):
            locked.current_color = ""
            fields.append("current_color")
            if not canonical_size_owned:
                locked.current_size = ""
                fields.append("current_size")
        locked.save(update_fields=fields)
        client.sales_context = context
        for field in fields:
            if field not in {"sales_context", "updated_at"}:
                setattr(client, field, getattr(locked, field))
        return {"persisted": True, "reason": "agreement_persisted", "agreement": agreement}


def _read_conversation_agreement(client, *, episode_id, source_namespace, reset_floor, watermark=None,
        _allow_expired_quote_for_retention=False, _allow_projection_changed_for_reprojection=False):
    """GET-only reverified agreement and source vector for a caller's fence."""
    from management.models import IgClient, IgCommercialEpisode, InstagramBotMessage
    from management.services.ig_conversation_routes import conversation_route_reset_floor
    from management.services.ig_memory_producer import _namespaces, _source_allowed
    from django.utils import timezone

    context = client.sales_context if isinstance(client.sales_context, dict) else {}
    agreement = context.get("conversation_agreement")
    unavailable = lambda reason: {"agreement": {}, "source_rows": [], "reason": reason}
    if not isinstance(agreement, dict) or agreement.get("schema") != SCHEMA:
        return unavailable("conversation_agreement_unavailable")
    expected = {"client_id": client.pk, "episode_id": episode_id,
        "reset_floor": reset_floor, "source_namespace": source_namespace}
    if (client.privacy_erasure_started_at is not None or client.current_commercial_episode_id != episode_id
            or agreement.get("scope") != expected or not source_namespace):
        return unavailable("conversation_agreement_scope_mismatch")
    current = IgClient.objects.filter(pk=client.pk).values("current_commercial_episode_id", "privacy_erasure_started_at", "igsid").first()
    if (not current or current["privacy_erasure_started_at"] is not None or current["current_commercial_episode_id"] != episode_id
            or current["igsid"] != client.igsid or conversation_route_reset_floor(client.pk) != reset_floor):
        return unavailable("conversation_agreement_scope_changed")
    episode_floor = (IgCommercialEpisode.objects.filter(pk=episode_id, client_id=client.pk)
        .values_list("opened_watermark_message_id", flat=True).first()) if episode_id is not None else 0
    if episode_id is not None and episode_floor is None:
        return unavailable("conversation_agreement_episode_unavailable")
    source_floor = max(reset_floor, int(episode_floor or 0))
    evidence = agreement.get("evidence") or {}
    ids = agreement.get("source_message_ids") or []
    transcript_ids = agreement.get("transcript_message_ids")
    if (not isinstance(evidence, dict) or not isinstance(ids, list) or len(ids) > MAX_MESSAGES
            or any(not isinstance(value, int) or isinstance(value, bool) or value < source_floor for value in ids)
            or len(set(ids)) != len(ids) or any(str(value) not in evidence for value in ids)):
        return unavailable("conversation_agreement_source_invalid")
    if (not isinstance(transcript_ids, list) or len(transcript_ids) > MAX_MESSAGES
            or any(not isinstance(value, int) or isinstance(value, bool) or value < source_floor for value in transcript_ids)
            or len(set(transcript_ids)) != len(transcript_ids) or not set(ids) <= set(transcript_ids)
            or transcript_ids != sorted(transcript_ids)):
        return unavailable("conversation_agreement_transcript_invalid")
    watermark_id = watermark.get("message_id") if isinstance(watermark, dict) else watermark
    watermark_at = watermark.get("event_at") if isinstance(watermark, dict) else None
    stored_watermark = agreement.get("watermark_message_id")
    if (not isinstance(stored_watermark, int) or isinstance(stored_watermark, bool)
            or (watermark is not None and (not isinstance(watermark_id, int) or isinstance(watermark_id, bool)))):
        return unavailable("conversation_agreement_watermark_unknown")
    if watermark_id is not None and (stored_watermark > watermark_id or any(value > watermark_id for value in transcript_ids)):
        return unavailable("conversation_agreement_after_capture")
    sources = list(InstagramBotMessage.objects.filter(client_id=client.pk, pk__in=transcript_ids).order_by("pk"))
    if len(sources) != len(transcript_ids):
        return unavailable("conversation_agreement_source_unavailable")
    rows = []
    namespaces = _namespaces(sources)
    for source in sources:
        row = {**_row(source), "provider_namespace": namespaces[source.pk]}
        proof = _proof(row)
        if (source.sender_id != client.igsid or namespaces[source.pk] != source_namespace or not _source_allowed(source)
                or (source.pk in ids and (proof != evidence.get(str(source.pk)) or source.status == "failed"
                    or (source.role.casefold() in _SELLERS | _BOTS and not _seller(row))))):
            return unavailable("conversation_agreement_source_changed")
        if watermark_at:
            try:
                if datetime.fromisoformat(proof["observed_at"]) > datetime.fromisoformat(watermark_at):
                    return unavailable("conversation_agreement_source_after_capture")
            except (ValueError, TypeError):
                return unavailable("conversation_agreement_source_time_unknown")
        row["event_at"] = proof["observed_at"]
        rows.append(row)
    reproduced = extract_conversation_agreement(rows)
    projection_changed = any(key != "watermark_message_id" and agreement.get(key) != value
        for key, value in reproduced.items())
    if projection_changed and not _allow_projection_changed_for_reprojection:
        return unavailable("conversation_agreement_projection_changed")
    expires_at = (agreement.get("amounts") or {}).get("expires_at")
    if expires_at and not _allow_expired_quote_for_retention:
        try:
            if datetime.fromisoformat(expires_at) <= timezone.now():
                return unavailable("conversation_agreement_quote_expired")
        except (TypeError, ValueError):
            return unavailable("conversation_agreement_quote_expiry_unknown")
    return {"agreement": deepcopy(agreement), "source_rows": rows, "reason": ""}


def agreement_projection_digest(agreement):
    """Canonical expected-head fence; contains no exported source facts."""
    return hashlib.sha256(json.dumps(agreement, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def reproject_conversation_agreement(client, messages, *, watermark, expected_agreement_digest):
    """Explicit source-verified refresh of a reviewed projection head.

    A parser upgrade may produce new facts from previously nonmaterial rows.
    This operation verifies old material proofs, then captures those current
    sources afresh. GET readers and ordinary observation cannot bypass a
    projection mismatch. Any refused fresh capture rolls back the prior head.
    """
    from django.db import transaction
    from django.utils import timezone
    from management.models import IgClient, InstagramBotMessage
    from management.services.ig_commercial_episodes import commercial_episode_client_lock
    from management.services.ig_conversation_routes import conversation_route_reset_floor
    from management.services.ig_admin_state_capture import _namespace

    unavailable = lambda reason: {"persisted": False, "reason": reason}
    supplied = list(messages or ())
    if (not isinstance(watermark, int) or isinstance(watermark, bool) or watermark <= 0
            or not supplied or len(supplied) > MAX_MESSAGES):
        return unavailable("agreement_reprojection_input_invalid")
    rows = [_row(row) for row in supplied]
    supplied_ids = [row["id"] for row in rows]
    if (not all(supplied_ids) or supplied_ids != sorted(set(supplied_ids)) or supplied_ids[-1] != watermark
            or not isinstance(expected_agreement_digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_agreement_digest)):
        return unavailable("agreement_reprojection_input_invalid")
    with commercial_episode_client_lock(client.pk), transaction.atomic():
        locked = IgClient.objects.select_for_update().filter(pk=client.pk).first()
        if locked is None or locked.privacy_erasure_started_at is not None:
            return unavailable("agreement_reprojection_client_unavailable")
        context = deepcopy(locked.sales_context) if isinstance(locked.sales_context, dict) else {}
        existing = context.get("conversation_agreement")
        if not isinstance(existing, dict) or existing.get("schema") != SCHEMA:
            return unavailable("agreement_reprojection_head_unavailable")
        prior_digest = agreement_projection_digest(existing)
        if prior_digest != expected_agreement_digest:
            return unavailable("agreement_reprojection_head_changed")
        scope = existing.get("scope") or {}
        floor = conversation_route_reset_floor(locked.pk)
        if (scope.get("client_id") != locked.pk or scope.get("episode_id") != locked.current_commercial_episode_id
                or scope.get("reset_floor") != floor or not scope.get("source_namespace")
                or scope.get("source_namespace") != _namespace()):
            return unavailable("agreement_reprojection_scope_changed")
        old_watermark = existing.get("watermark_message_id")
        if (not isinstance(old_watermark, int) or isinstance(old_watermark, bool) or old_watermark > watermark):
            return unavailable("agreement_reprojection_watermark_changed")
        old_ids = existing.get("transcript_message_ids") or []
        if (not isinstance(old_ids, list) or len(old_ids) > MAX_MESSAGES
                or any(not isinstance(value, int) or isinstance(value, bool) for value in old_ids)):
            return unavailable("agreement_reprojection_sources_invalid")
        # Source edits must not race the old-proof check and fresh capture.
        list(InstagramBotMessage.objects.select_for_update().filter(client_id=locked.pk,
            pk__in=set(old_ids) | set(supplied_ids)).order_by("pk"))
        verified = _read_conversation_agreement(locked, episode_id=scope["episode_id"],
            source_namespace=scope["source_namespace"], reset_floor=floor, watermark=old_watermark,
            _allow_projection_changed_for_reprojection=True)
        if verified["reason"]:
            return unavailable("agreement_reprojection_" + verified["reason"])
        # The supplied source window must retain the whole reviewed head;
        # otherwise a refresh could accidentally discard durable evidence.
        if not set(old_ids) <= set(supplied_ids):
            return unavailable("agreement_reprojection_history_incomplete")
        context.pop("conversation_agreement")
        locked.sales_context = context
        locked.save(update_fields=["sales_context", "updated_at"])
        captured = persist_conversation_agreement(locked, rows, watermark=watermark)
        if not captured["persisted"]:
            transaction.set_rollback(True)
            return unavailable("agreement_reprojection_" + captured["reason"])
        if captured["agreement"].get("scope") != scope:
            transaction.set_rollback(True)
            return unavailable("agreement_reprojection_scope_changed")
        new_digest = agreement_projection_digest(captured["agreement"])
        audit = context.get("conversation_agreement_reprojections")
        audit = audit[-9:] if isinstance(audit, list) else []
        # Only controlled, bounded metadata from previous entries survives.
        allowed = {"prior_digest", "new_digest", "prior_source_message_ids", "source_message_ids", "watermark_message_id", "applied_at", "reason"}
        audit = [{key: entry[key] for key in allowed} for entry in audit
            if isinstance(entry, dict) and allowed <= entry.keys()
            and entry["reason"] == "explicit_source_reprojection"
            and all(isinstance(entry[key], str) and re.fullmatch(r"[0-9a-f]{64}", entry[key]) for key in ("prior_digest", "new_digest"))
            and isinstance(entry["applied_at"], str) and len(entry["applied_at"]) <= 40
            and re.fullmatch(r"[\dT:+.\-]+", entry["applied_at"])
            and isinstance(entry["watermark_message_id"], int) and not isinstance(entry["watermark_message_id"], bool)
            and all(isinstance(entry[key], list) and len(entry[key]) <= MAX_MESSAGES
                and all(isinstance(value, int) and not isinstance(value, bool) for value in entry[key])
                for key in ("prior_source_message_ids", "source_message_ids"))]
        audit.append({"prior_digest": prior_digest, "new_digest": new_digest,
            "prior_source_message_ids": list(existing.get("source_message_ids") or []),
            "source_message_ids": list(captured["agreement"].get("source_message_ids") or []),
            "watermark_message_id": watermark, "applied_at": timezone.now().isoformat(),
            "reason": "explicit_source_reprojection"})
        context = dict(locked.sales_context)
        context["conversation_agreement_reprojections"] = audit
        locked.sales_context = context
        locked.save(update_fields=["sales_context", "updated_at"])
        client.sales_context = context
        for field in ("current_size", "current_color", "current_qty"):
            setattr(client, field, getattr(locked, field))
        return {**captured, "reason": "agreement_reprojected", "prior_digest": prior_digest, "new_digest": new_digest}


def read_conversation_agreement(client, *, episode_id, source_namespace, reset_floor, watermark=None):
    """Source-verified current read; expired quotations remain inadmissible."""
    return _read_conversation_agreement(client, episode_id=episode_id,
        source_namespace=source_namespace, reset_floor=reset_floor, watermark=watermark)


def initial_agreement_transfer_sources(client):
    """Read proof for the sole pre-episode → first-review scope transition.

    The episode owner must call this under its client lock BEFORE creating the
    first episode. This does not create an episode or lower an existing floor.
    The owner may bind precisely these messages to its new first episode and
    persist them with ``persist_conversation_agreement`` in the same transaction.
    """
    from management.models import IgClient, IgCommercialEpisode, IgDeal, IgOrderAttribution, InstagramBotMessage
    from management.services.ig_admin_state_capture import _namespace
    from management.services.ig_conversation_routes import conversation_route_reset_floor
    from management.services.ig_memory_producer import _namespaces, _source_allowed

    empty = lambda reason: {"messages": [], "agreement": {}, "source_floor": None,
        "source_message_ids": [], "agreement_digest": "", "reason": reason}
    current = IgClient.objects.filter(pk=client.pk).first()
    if current is None or current.privacy_erasure_started_at is not None or current.current_commercial_episode_id is not None:
        return empty("initial_agreement_scope_unavailable")
    if (IgCommercialEpisode.objects.filter(client_id=current.pk).exists()
            or IgDeal.objects.filter(client_id=current.pk).exists()
            or IgOrderAttribution.objects.filter(client_id=current.pk).exists()):
        return empty("initial_agreement_prior_commercial_history")
    namespace, floor = _namespace(), conversation_route_reset_floor(current.pk)
    captured = read_conversation_agreement(current, episode_id=None, source_namespace=namespace, reset_floor=floor)
    agreement, rows = captured["agreement"], captured["source_rows"]
    if captured["reason"]:
        return empty(captured["reason"])
    if not agreement.get("items") or not rows:
        return empty("initial_agreement_configuration_unavailable")
    latest = list(InstagramBotMessage.objects.filter(client_id=current.pk, sender_id=current.igsid,
        pk__gte=floor, role__in=("user", "manager", "model")).exclude(status="failed").order_by("-pk")[:MAX_MESSAGES])
    namespaces = _namespaces(latest)
    if any(_source_allowed(source) and source.pk > agreement["watermark_message_id"] for source in latest):
        return empty("initial_agreement_observation_stale")
    if any(_source_allowed(source) and source.pk > min(row["id"] for row in rows) and namespaces[source.pk] != namespace for source in latest):
        return empty("initial_agreement_namespace_changed")
    digest = hashlib.sha256(json.dumps(agreement, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    return {"messages": rows, "agreement": agreement, "source_floor": min(row["id"] for row in rows),
        "source_message_ids": [row["id"] for row in rows], "agreement_digest": digest, "reason": ""}
