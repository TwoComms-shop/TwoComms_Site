"""Pure deterministic truth validation for final Instagram bot replies."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
from typing import Iterable, Literal


REASON_CODES = (
    "unauthorized_url",
    "unsupported_currency",
    "unverified_price",
    "unverified_discount",
    "unverified_payment",
    "unverified_order",
    "unverified_shipment",
    "unverified_tracking",
    "unverified_timing",
    "unverified_recruitment",
    "configuration_mismatch",
    "unauthorized_action",
)


@dataclass(frozen=True)
class ProposedAction:
    kind: str
    value: str | bool


@dataclass(frozen=True)
class AuthorizedAction:
    kind: str
    value: str | bool


@dataclass(frozen=True)
class ReplyTruthContext:
    authorized_prices: tuple[Decimal, ...] = ()
    authorized_price_ranges: tuple[tuple[Decimal, Decimal], ...] = ()
    allowed_currency_codes: tuple[str, ...] = ("UAH",)
    authorized_urls: tuple[str, ...] = ()
    authorized_discount_percents: tuple[Decimal, ...] = ()
    authorized_discount_amounts: tuple[Decimal, ...] = ()
    payment_confirmed: bool = False
    order_created: bool = False
    shipment_state: str = "unknown"
    known_tracking_refs: tuple[str, ...] = ()
    approved_timing_claims: tuple[str, ...] = ()
    explicitly_qualified_standard_dispatch_days: tuple[int, int] | None = None
    allowed_sizes: tuple[str, ...] = ()
    allowed_fits: tuple[str, ...] = ()
    allowed_colors: tuple[str, ...] = ()
    authorized_actions: tuple[AuthorizedAction, ...] = ()
    quoted_data: tuple[str, ...] = ()
    # Future recruitment-policy integration must supply explicit source-backed
    # authority. Missing policy, customer text and model controls leave unknown.
    recruitment_status: Literal["unknown", "open", "closed"] = "unknown"


@dataclass(frozen=True)
class ReplyTruthResult:
    valid: bool
    reasons: tuple[str, ...] = ()


_URL_RE = re.compile(r"https?://[^\s<>]+", re.I)
_SENTENCE_BOUNDARY_RE = re.compile(
    r"(?<=[!?])\s+|(?<!\d)\.(?=\s|$)|\n+",
    re.U,
)
_QUESTION_CLAUSE_BOUNDARY_RE = re.compile(r"[,;]|\s+[—–-]\s+")
_QUOTED_SPAN_RE = re.compile(
    r'"[^"\n]{1,2000}"|«[^»\n]{1,2000}»|“[^”\n]{1,2000}”|‘[^’\n]{1,2000}’'
)
_NEGATION_SUFFIX_RE = re.compile(
    r"\b(?:не|ні|нет|немає|not|no|isn['’]?t|hasn['’]?t|never)\s*$",
    re.I,
)
_NEGATED_STATUS_RE = re.compile(
    r"\b(?:не|нет|not|isn['’]?t|hasn['’]?t)\s+(?:ще\s+|ещ[её]\s+|yet\s+)?"
    r"(?:(?:була|був|було|були|была|был|было|были|was|were|been)\s+)?"
    r"(?:підтвердж\w*|подтвержд\w*|отрим\w*|получ\w*|зарах\w*|"
    r"створен\w*|создан\w*|оформлен\w*|відправлен\w*|отправлен\w*|"
    r"доставлен\w*|confirmed|received|created|placed|shipped|dispatched|delivered)",
    re.I,
)
_NEGATED_DISCOUNT_RE = re.compile(
    r"\b(?:не|нет|not|no)\b[^.!?\n]{0,40}(?:зниж\w*|скид\w*|discount)"
    r"|(?:зниж\w*|скид\w*|discount)[^.!?\n]{0,30}"
    r"(?:не\s+буде|не\s+будет|немає|нет|not\s+available|no\s+discount)",
    re.I,
)
_VACANCY_NOUN = r"(?:ваканс\w*|vacanc(?:y|ies)|(?:job\s+)?positions?|job\s+openings?)"
_RECRUITMENT_ROLE = r"(?:менеджер\w*|працівник\w*|співробітник\w*|сотрудник\w*|персонал\w*|manager\w*|staff|employees?)"
_RECRUITMENT_STATUS_RE = re.compile(
    r"(?P<closed>"
    r"\b(?:не\s+(?:шукаємо|ищем|наймаємо|нанимаем)\s+" + _RECRUITMENT_ROLE
    + r"|(?:немає|нема|нет)\s+(?:(?:відкрит\w*|открыт\w*|актуальн\w*)\s+)?" + _VACANCY_NOUN
    + r"|" + _VACANCY_NOUN + r"\s+(?:немає|нема|нет|відсутн\w*|закрит\w*|закрыт\w*)"
    r"|we(?:(?:\s+are|['’]re)\s+not|\s+aren['’]?t)\s+(?:currently\s+)?hiring"
    r"|we\s+(?:are\s+not|aren['’]?t)\s+looking\s+for\s+" + _RECRUITMENT_ROLE
    + r"|no\s+(?:open\s+)?" + _VACANCY_NOUN
    + r"|" + _VACANCY_NOUN + r"\s+(?:are\s+)?(?:closed|unavailable|not\s+(?:open|available)))\b"
    r")|(?P<open>"
    r"\b(?:(?:шукаємо|ищем|наймаємо|нанимаем)\s+" + _RECRUITMENT_ROLE
    + r"|(?:є|есть)\s+(?:(?:відкрит\w*|открыт\w*|актуальн\w*)\s+)?" + _VACANCY_NOUN
    + r"|(?:відкрит\w*|открыт\w*)\s+" + _VACANCY_NOUN
    + r"|" + _VACANCY_NOUN + r"\s+(?:відкрит\w*|открыт\w*|є|есть)"
    r"|we(?:\s+are|['’]re)\s+(?:currently\s+)?hiring"
    r"|we(?:\s+are|['’]re)\s+looking\s+for\s+" + _RECRUITMENT_ROLE
    + r"|(?:we\s+have|there\s+are)\s+(?:open\s+)?" + _VACANCY_NOUN
    + r"|" + _VACANCY_NOUN + r"\s+(?:are\s+)?(?:open|available))\b"
    r")", re.I,
)
_RECRUITMENT_CLAUSE_BOUNDARY_RE = re.compile(r";|\s+(?:але|однак|но|однако|but|however)\s+", re.I)
_RECRUITMENT_UNCERTAINTY_RE = re.compile(
    r"(?:не\s+(?:можу|можемо|могу|можем)\s+(?:підтвердити|подтвердить)"
    r"|не\s+(?:знаю|знаємо|знаем)"
    r"|(?:cannot|can['’]?t|can\s+not)\s+confirm"
    r"|(?:do\s+not|don['’]?t)\s+know)"
    r"\s*,?\s*[^,;.!?]{0,80}$", re.I,
)
_MONEY_RE = re.compile(
    r"(?<!\d)(?P<amount>\d{1,9}(?:[.,]\d{1,2})?)\s*"
    r"(?P<currency>грн|₴|uah|usd|eur|gbp|pln|cad|aud|chf|jpy|"
    r"\$|€|£|zł)(?!\w)",
    re.I,
)
_PREFIX_MONEY_RE = re.compile(
    r"(?<!\w)(?P<currency>грн|₴|uah|usd|eur|gbp|pln|cad|aud|chf|jpy|"
    r"\$|€|£|zł)\s*(?P<amount>\d{1,9}(?:[.,]\d{1,2})?)(?!\d)",
    re.I,
)
_RANGE_RE = re.compile(
    r"\b(?:від|от|from)\s*(?P<low>\d{1,9}(?:[.,]\d{1,2})?)\s*"
    r"(?:грн|₴|uah)?\s*(?:до|to|[-–—])\s*"
    r"(?P<high>\d{1,9}(?:[.,]\d{1,2})?)\s*"
    r"(?P<currency>грн|₴|uah|usd|eur|gbp|pln|cad|aud|chf|jpy|"
    r"\$|€|£|zł)",
    re.I,
)
_CURRENCY_TOKEN_RE = re.compile(
    r"(?<!\w)(uah|usd|eur|gbp|pln|cad|aud|chf|jpy|грн|₴|\$|€|£|zł)(?!\w)",
    re.I,
)
_DISCOUNT_RE = re.compile(r"зниж\w*|скид\w*|discount", re.I)
_PERCENT_RE = re.compile(r"(?<!\d)(\d{1,3}(?:[.,]\d{1,2})?)\s*%")
_PAYMENT_CLAIM_RE = re.compile(
    r"(?:оплат\w*|платіж\w*|платеж\w*|payment)[^.!?\n]{0,70}"
    r"(?:підтвердж\w*|подтвержд\w*|отрим\w*|получ\w*|зарах\w*|"
    r"successful|confirmed|received|completed|пройш\w*|прош\w*)"
    r"|(?:підтвердж\w*|подтвержд\w*|отрим\w*|получ\w*|зарах\w*|"
    r"successful|confirmed|received|completed)[^.!?\n]{0,70}"
    r"(?:оплат\w*|платіж\w*|платеж\w*|payment)"
    r"|\b(?:paid|оплачено|сплачено)\b",
    re.I,
)
_CONDITIONAL_PAYMENT_RE = re.compile(
    r"(?:(?:після\s+підтвердж\w*|после\s+подтвержд\w*)"
    r"[^.!?]{0,120}оплат\w*[^.!?]{0,50}(?:відкри\w*|откро\w*)|"
    r"after\s+(?:you\s+)?confirm\w*[^.!?]{0,120}"
    r"payment[^.!?]{0,50}opens?)",
    re.I,
)
_ORDER_CLAIM_RE = re.compile(
    r"(?:замовлен\w*|заказ\w*|order)[^.!?\n]{0,70}"
    r"(?:створен\w*|создан\w*|оформлен\w*|placed|created|confirmed)"
    r"|(?:створен\w*|создан\w*|оформлен\w*|placed|created|confirmed)"
    r"[^.!?\n]{0,70}(?:замовлен\w*|заказ\w*|order)",
    re.I,
)
_SHIPPED_RE = re.compile(
    r"\b(?:відправлен(?:о|ий|а|е|і)|"
    r"отправлен(?:о|а|ы|ный|ная|ное|ные)?|shipped|dispatched)\b",
    re.I,
)
_DELIVERED_RE = re.compile(r"\b(?:доставлен\w*|отриман\w*|получен\w*|delivered)\b", re.I)
_READY_TO_SHIP_RE = re.compile(
    r"\b(?:(?:готов|готовий|готова|готове|готові|готово|готовы)\s+"
    r"(?:до|к)\s+(?:відправ\w*|отправ\w*)|ready\s+to\s+ship)\b",
    re.I,
)
_SHIPMENT_CONTEXT_RE = re.compile(
    r"посилк\w*|посылк\w*|відправ\w*|отправ\w*|достав\w*|shipment|"
    r"parcel|package|dispatch\w*|shipping|замовлен\w*|заказ\w*|order",
    re.I,
)
_TRACKING_RE = re.compile(
    r"\b(?:ттн|tracking(?:\s+(?:number|id))?)\s*[:#№-]?\s*([A-Za-z0-9-]{5,40})\b",
    re.I,
)
_TIMING_CONTEXT_RE = re.compile(
    r"достав\w*|відправ\w*|отправ\w*|виготов\w*|изготов\w*|вироб\w*|"
    r"підготов\w*|подготов\w*|готу\w*|готов\w*|prepar\w*|"
    r"production|deliver\w*|ship\w*|dispatch\w*",
    re.I,
)
_TIMING_RE = re.compile(
    r"\b(?:"
    r"(?:до\s+|within\s+|in\s+)?\d{1,3}(?:\s*[-–—]\s*\d{1,3})?\s*"
    r"(?:(?:робоч\w*|рабоч\w*|business)\s+)?"
    r"(?:хвилин\w*|минут\w*|minutes?|годин\w*|час\w*|hours?|"
    r"дн(?:і|ів|я)|день|дня|дней|days?|тижн\w*|недел\w*|weeks?)"
    r"|(?:сьогодні|завтра|послезавтра|today|tomorrow)"
    r"|(?:до|by|on)\s+(?:\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?"
    r"|\d{1,2}\s+[^\W\d_]{3,16})"
    r")\b",
    re.I,
)
_STANDARD_TIMING_RANGE_RE = re.compile(
    r"(?<!\d)(?P<low>\d{1,3})\s*[-–—]\s*(?P<high>\d{1,3})\s*"
    r"(?:(?:робоч\w*|рабоч\w*|business)\s+)?"
    r"(?:дн(?:і|ів|я)|день|дня|дней|days?)\b",
    re.I,
)
_USUAL_QUALIFIER_RE = re.compile(
    r"\b(?:зазвичай|обычно|usually|typically)\b",
    re.I,
)
_PREPARATION_OR_DISPATCH_RE = re.compile(
    r"підготов\w*|готу\w*|подготов\w*|готов\w*|"
    r"відправ\w*|отправ\w*|dispatch\w*|ship\w*",
    re.I,
)
_AFTER_PAYMENT_RE = re.compile(
    r"(?:після|после)\s+(?:підтвердж\w*\s+|подтвержд\w*\s+)?оплат\w*|"
    r"after\s+(?:confirmed\s+)?payment",
    re.I,
)
_TIMING_GUARANTEE_RE = re.compile(
    r"гарант\w*|обіця\w*|обещ\w*|guarantee\w*|definitely|точно",
    re.I,
)
_CARRIER_DELIVERY_RE = re.compile(
    r"достав\w*|перевізник\w*|перевозчик\w*|carrier\w*|transit|"
    r"arrival|arriv\w*|прибут\w*|нова\s+пошта|нов\w*\s+почт\w*|nova\s+poshta",
    re.I,
)
_SELECTION_WORDS = (
    r"(?:обран(?:ий|а|е|і)|вибран(?:ий|а|е|і)|"
    r"выбран(?:ный|ная|ное|ные)|selected|chosen)"
)
_SIZE_RE = re.compile(
    rf"\b(?:(?P<selected>{_SELECTION_WORDS})\s+)?"
    r"(?:розмір|размер|size)\s*(?P<separator>[:=-])?\s*"
    r"(?P<value>[A-Za-z0-9_-]{1,16})\b",
    re.I,
)
_FIT_RE = re.compile(
    rf"\b(?:(?P<selected>{_SELECTION_WORDS})\s+)?"
    r"(?:крій|крой|fit)\s*(?P<separator>[:=-])?\s*"
    r"(?P<value>[A-Za-z0-9_-]{1,32})\b",
    re.I,
)
_COLOR_RE = re.compile(
    rf"\b(?:(?P<selected>{_SELECTION_WORDS})\s+)?"
    r"(?:колір|цвет|color)\s*(?P<separator>[:=-])?\s*"
    r"(?P<value>[\w-]{2,40})\b",
    re.I,
)
_NON_CONFIGURATION_VALUES = frozenset({
    "depends", "залежить", "зависит", "впливає", "влияет", "varies",
    "chart", "таблиця", "таблица", "на", "on",
})
_CONFIGURATION_DESCRIPTION_RE = re.compile(
    r"\b(?:залеж\w*|завис\w*|depends?|varies?|"
    r"відрізня\w*|отлича\w*|differ\w*|"
    r"is\s+on\s+(?:the\s+)?product\s+page)\b",
    re.I,
)


def _decimal(value) -> Decimal | None:
    try:
        return Decimal(str(value).replace(",", ".")).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _normalize(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def _currency_code(value: str) -> str:
    token = str(value or "").casefold()
    return {
        "грн": "UAH", "₴": "UAH", "uah": "UAH",
        "usd": "USD", "$": "USD", "eur": "EUR", "€": "EUR",
        "gbp": "GBP", "£": "GBP", "pln": "PLN", "zł": "PLN",
    }.get(token, token.upper())


def _claim_sentences(text: str, quoted_data: Iterable[str]):
    claims = str(text or "")
    quoted_values = {_normalize(value) for value in quoted_data if value}
    if quoted_values:
        claims = _QUOTED_SPAN_RE.sub(
            lambda match: (
                " " * len(match.group(0))
                if _normalize(match.group(0)[1:-1]) in quoted_values
                else match.group(0)
            ),
            claims,
        )
    for raw_sentence in _SENTENCE_BOUNDARY_RE.split(claims):
        sentence = raw_sentence.strip()
        if not sentence:
            continue
        if "?" not in sentence and "？" not in sentence:
            yield sentence
            continue
        for clause in _QUESTION_CLAUSE_BOUNDARY_RE.split(sentence):
            clause = clause.strip()
            if clause and "?" not in clause and "？" not in clause:
                yield clause


def _locally_negated(sentence: str, start: int) -> bool:
    clause = sentence[max(0, start - 32):start]
    clause = re.split(r"[,;:]", clause)[-1]
    return bool(_NEGATION_SUFFIX_RE.search(clause))


def _has_positive_claim(pattern: re.Pattern, sentence: str) -> bool:
    return any(
        not _locally_negated(sentence, match.start())
        and not _NEGATED_STATUS_RE.search(match.group(0))
        for match in pattern.finditer(sentence)
    )


def _recruitment_claims(sentence: str):
    # Closure is itself a protected assertion; the generic negation helper must
    # not turn "we are not hiring" into permission to invent a closed policy.
    for clause in _RECRUITMENT_CLAUSE_BOUNDARY_RE.split(sentence):
        for match in _RECRUITMENT_STATUS_RE.finditer(clause):
            if not _RECRUITMENT_UNCERTAINTY_RE.search(clause[:match.start()]):
                yield match.lastgroup


def _add(reasons: list[str], reason: str) -> None:
    if reason in REASON_CODES and reason not in reasons:
        reasons.append(reason)


def _positive_configuration_value(sentence: str, match: re.Match) -> str:
    """Return an asserted selection, excluding descriptive label phrases."""
    claimed = str(match.group("value") or "")
    if _normalize(claimed) in _NON_CONFIGURATION_VALUES:
        return ""
    if match.group("selected") or match.group("separator"):
        return claimed
    local_tail = re.split(r"[,;.!?]", sentence[match.end():], maxsplit=1)[0]
    if _CONFIGURATION_DESCRIPTION_RE.search(local_tail[:120]):
        return ""
    return claimed


def _is_qualified_standard_dispatch_timing(
    sentence: str,
    timing: str,
    context: ReplyTruthContext,
) -> bool:
    configured = context.explicitly_qualified_standard_dispatch_days
    if (
        not isinstance(configured, tuple)
        or len(configured) != 2
        or any(isinstance(value, bool) or not isinstance(value, int) for value in configured)
    ):
        return False
    match = _STANDARD_TIMING_RANGE_RE.fullmatch(str(timing or "").strip())
    if match is None or (int(match.group("low")), int(match.group("high"))) != configured:
        return False
    if (
        not _USUAL_QUALIFIER_RE.search(sentence)
        or not _PREPARATION_OR_DISPATCH_RE.search(sentence)
        or _TIMING_GUARANTEE_RE.search(sentence)
        or _CARRIER_DELIVERY_RE.search(sentence)
    ):
        return False
    return bool(context.payment_confirmed or _AFTER_PAYMENT_RE.search(sentence))


def validate_reply_truth(
    reply_text: str,
    *,
    actions: Iterable[ProposedAction] = (),
    context: ReplyTruthContext,
) -> ReplyTruthResult:
    """Validate protected positive assertions against an explicit snapshot."""
    reasons: list[str] = []
    allowed_urls = set(context.authorized_urls)
    for match in _URL_RE.finditer(str(reply_text or "")):
        url = match.group(0).rstrip(".,;:!?)]}")
        if url not in allowed_urls:
            _add(reasons, "unauthorized_url")

    allowed_actions = {
        (_normalize(item.kind), _normalize(item.value))
        for item in context.authorized_actions
    }
    for action in actions:
        if (_normalize(action.kind), _normalize(action.value)) not in allowed_actions:
            _add(reasons, "unauthorized_action")

    allowed_currencies = {code.upper() for code in context.allowed_currency_codes}
    prices = {_decimal(value) for value in context.authorized_prices}
    prices.discard(None)
    ranges = {
        (_decimal(low), _decimal(high))
        for low, high in context.authorized_price_ranges
    }
    discount_percents = {_decimal(value) for value in context.authorized_discount_percents}
    discount_amounts = {_decimal(value) for value in context.authorized_discount_amounts}
    approved_timing = {_normalize(value) for value in context.approved_timing_claims}

    for sentence in _claim_sentences(reply_text, context.quoted_data):
        for recruitment_status in _recruitment_claims(sentence):
            if context.recruitment_status != recruitment_status:
                _add(reasons, "unverified_recruitment")
        range_spans = []
        for match in _RANGE_RE.finditer(sentence):
            if _locally_negated(sentence, match.start()):
                continue
            range_spans.append(match.span())
            currency = _currency_code(match.group("currency"))
            if currency not in allowed_currencies:
                _add(reasons, "unsupported_currency")
                continue
            pair = (_decimal(match.group("low")), _decimal(match.group("high")))
            if pair not in ranges:
                _add(reasons, "unverified_price")

        is_discount = bool(_DISCOUNT_RE.search(sentence))
        discount_negated = bool(_NEGATED_DISCOUNT_RE.search(sentence))
        if is_discount and not discount_negated:
            for match in _PERCENT_RE.finditer(sentence):
                if _decimal(match.group(1)) not in discount_percents:
                    _add(reasons, "unverified_discount")
        for match in (*_MONEY_RE.finditer(sentence), *_PREFIX_MONEY_RE.finditer(sentence)):
            if any(start <= match.start() < end for start, end in range_spans):
                continue
            if _locally_negated(sentence, match.start()):
                continue
            currency = _currency_code(match.group("currency"))
            if currency not in allowed_currencies:
                _add(reasons, "unsupported_currency")
                continue
            amount = _decimal(match.group("amount"))
            if is_discount and not discount_negated:
                if amount not in discount_amounts:
                    _add(reasons, "unverified_discount")
            elif discount_negated:
                continue
            elif amount not in prices:
                _add(reasons, "unverified_price")
        for token in _CURRENCY_TOKEN_RE.findall(sentence):
            if _currency_code(token) not in allowed_currencies:
                _add(reasons, "unsupported_currency")

        if (
            _has_positive_claim(_PAYMENT_CLAIM_RE, sentence)
            and not _CONDITIONAL_PAYMENT_RE.search(sentence)
            and not context.payment_confirmed
        ):
            _add(reasons, "unverified_payment")
        if _has_positive_claim(_ORDER_CLAIM_RE, sentence) and not context.order_created:
            _add(reasons, "unverified_order")
        shipping_context = bool(_SHIPMENT_CONTEXT_RE.search(sentence))
        if shipping_context and _has_positive_claim(_DELIVERED_RE, sentence):
            if context.shipment_state != "delivered":
                _add(reasons, "unverified_shipment")
        elif shipping_context and _has_positive_claim(_SHIPPED_RE, sentence):
            if context.shipment_state not in {"shipped", "delivered"}:
                _add(reasons, "unverified_shipment")
        elif shipping_context and _has_positive_claim(_READY_TO_SHIP_RE, sentence):
            if context.shipment_state not in {"ready", "shipped", "delivered"}:
                _add(reasons, "unverified_shipment")
        for tracking in _TRACKING_RE.findall(sentence):
            if tracking not in set(context.known_tracking_refs):
                _add(reasons, "unverified_tracking")

        if _TIMING_CONTEXT_RE.search(sentence):
            for timing_match in _TIMING_RE.finditer(sentence):
                if _locally_negated(sentence, timing_match.start()):
                    continue
                timing = timing_match.group(0)
                if (
                    _normalize(timing) not in approved_timing
                    and not _is_qualified_standard_dispatch_timing(
                        sentence,
                        timing,
                        context,
                    )
                ):
                    _add(reasons, "unverified_timing")

        for pattern, allowed in (
            (_SIZE_RE, context.allowed_sizes),
            (_FIT_RE, context.allowed_fits),
            (_COLOR_RE, context.allowed_colors),
        ):
            allowed_values = {_normalize(value) for value in allowed}
            for match in pattern.finditer(sentence):
                if _locally_negated(sentence, match.start()):
                    continue
                claimed = _positive_configuration_value(sentence, match)
                if not claimed:
                    continue
                if _normalize(claimed) not in allowed_values:
                    _add(reasons, "configuration_mismatch")

    return ReplyTruthResult(valid=not reasons, reasons=tuple(reasons))


__all__ = [
    "AuthorizedAction",
    "ProposedAction",
    "REASON_CODES",
    "ReplyTruthContext",
    "ReplyTruthResult",
    "validate_reply_truth",
]
