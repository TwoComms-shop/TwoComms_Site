"""Pure deterministic truth validation for final Instagram bot replies."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from itertools import islice
import json
import re
from typing import Iterable, Literal
from zoneinfo import ZoneInfo

from management.services.ig_fulfillment_requirements import _MONTHS


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
    "unverified_availability",
    "unauthorized_action",
    "unverified_shipping",
    "unverified_paid_basket",
    "unverified_product_material",
    "unverified_product_quality",
    "unverified_product_story",
    "unverified_payment_policy",
)

CORRECTED_REQUIREMENT_RE = re.compile(
    r"уточн[её]н\w*|скоригован\w*|скорректирован\w*|corrected|updated requirement", re.I,
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
class TimingAuthority:
    """A source-backed timing fact; durations never grant another domain."""

    domain: Literal["preparation", "dispatch", "base_arrival", "carrier_estimate", "arrival_feasibility"]
    claim: str = ""
    min_days: int | None = None
    max_days: int | None = None
    unit: str = "calendar_days"
    anchor: str = ""
    anchor_at: str = ""
    line_id: str = ""
    line_label: str = ""
    source_ref: str = ""
    review_line_labels: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class FulfillmentDeadlineAuthority:
    """One reviewed customer cutoff; never positive arrival or stock authority."""

    requirement_digest: str
    deadline_at: str
    deadline_kind: Literal["departure", "arrival", "dispatch"]
    outcome: Literal["impossible", "review_pending"]
    line_ids: tuple[str, ...]
    source_message_id: int
    source_ref: str
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class CheckoutLineAuthority:
    line_id: str
    title: str
    quantity: int
    unit_price: Decimal
    line_total: Decimal
    size: str = ""
    fit: str = ""
    color: str = ""


@dataclass(frozen=True)
class ShippingAuthority:
    """Verified payer/policy facts; thresholds never enter goods price pools."""
    source_ref: str
    mode: str
    payer_type: str
    authority: str
    currency: str = "UAH"
    phase: str = ""
    customer_maximum: Decimal | None = None
    customer_charge: Decimal | None = None
    delivery_prepaid: bool = False
    policy_version: str = ""
    threshold: Decimal | None = None
    basis_total: Decimal | None = None
    eligible: bool | None = None


@dataclass(frozen=True)
class CheckoutOrderAuthority:
    """Detached native winning Order, with the complete signed wire snapshot."""
    client_id: int
    episode_id: int
    order_id: int
    order_number: str
    winner_generation_id: int | None
    attempt_id: int
    observed_at: str
    items: tuple[CheckoutLineAuthority, ...]
    gross: Decimal
    discount: Decimal
    merchandise_payable: Decimal
    payable: Decimal
    confirmed_paid: Decimal | None
    balance: Decimal | None
    shipping_authorities: tuple[ShippingAuthority, ...] = ()
    _snapshot_json: str = "{}"

    @property
    def fully_paid(self):
        return bool(self.confirmed_paid is not None and self.balance == 0 and self.payable > 0
            and self.confirmed_paid >= self.payable)

    def as_dict(self):
        return json.loads(self._snapshot_json)


@dataclass(frozen=True)
class AvailabilityAuthority:
    """A manager decision about an exact requested configuration and count."""

    line_id: str
    line_label: str
    product_id: int
    color_variant_id: int
    size: str
    fit_code: str
    quantity: int
    availability: str
    checked_quantity: int | None = None
    supply_basis: str = ""
    source_ref: str = ""
    product_label: str = ""
    color_label: str = ""
    fit_label: str = ""
    option_values: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class InventoryAvailabilityAuthority(AvailabilityAuthority):
    """Positive exact retail inventory observation; no manager authorship."""

    inventory_digest: str = ""
    configuration_digest: str = ""
    source_cart_capture_digest: str = ""
    capture_digest: str = ""


@dataclass(frozen=True)
class SourcePriceSubject:
    """Validator-only identity of one captured source line's price subject."""

    line_id: str
    product_id: int
    title: str
    capture_digest: str
    source_message_ids: tuple[int, ...]
    choices: tuple[tuple[str, str], ...]
    prices: tuple[Decimal, ...]
    product_kind: str = ""


@dataclass(frozen=True)
class ReplyTruthContext:
    authorized_prices: tuple[Decimal, ...] = ()
    authorized_price_ranges: tuple[tuple[Decimal, Decimal], ...] = ()
    allowed_currency_codes: tuple[str, ...] = ("UAH",)
    authorized_urls: tuple[str, ...] = ()
    authorized_discount_percents: tuple[Decimal, ...] = ()
    authorized_discount_amounts: tuple[Decimal, ...] = ()
    # Backend-only exact public explanations, never an applied discount or
    # payment/order capability; dictionaries and customer prose are rejected.
    cooperation_policy: object | None = None
    payment_confirmed: bool = False
    order_created: bool = False
    shipment_state: str = "unknown"
    known_tracking_refs: tuple[str, ...] = ()
    approved_timing_claims: tuple[str, ...] = ()
    explicitly_qualified_standard_dispatch_days: tuple[int, int] | None = None
    allowed_sizes: tuple[str, ...] = ()
    allowed_fits: tuple[str, ...] = ()
    allowed_colors: tuple[str, ...] = ()
    source_chosen_sizes: tuple[str, ...] = ()
    source_chosen_fits: tuple[str, ...] = ()
    source_chosen_colors: tuple[str, ...] = ()
    audited_chosen_sizes: tuple[str, ...] = ()
    authorized_actions: tuple[AuthorizedAction, ...] = ()
    quoted_data: tuple[str, ...] = ()
    # Seller agreement components authorize attributed conversation statements,
    # never catalogue prices or payment settlement. Tuple: kind, amount,
    # currency, source message ID, agreement authority.
    conversation_amounts: tuple[tuple[str, str, str, int, str], ...] = ()
    agreement_choices: tuple[tuple[str, str, int], ...] = ()
    # Future recruitment-policy integration must supply explicit source-backed
    # authority. Missing policy, customer text and model controls leave unknown.
    recruitment_status: Literal["unknown", "open", "closed"] = "unknown"
    timing_authorities: tuple[TimingAuthority, ...] = ()
    fulfillment_deadline_authorities: tuple[FulfillmentDeadlineAuthority, ...] = ()
    fulfillment_review_status: Literal["", "pending", "reviewed"] = ""
    availability_authorities: tuple[AvailabilityAuthority, ...] = ()
    inventory_availability_authorities: tuple[InventoryAvailabilityAuthority, ...] = ()
    inventory_cart_line_ids: tuple[str, ...] = ()
    checkout_authority: CheckoutOrderAuthority | None = None
    full_payment_confirmed: bool | None = None
    shipping_authorities: tuple[ShippingAuthority, ...] = ()
    approved_product_assertions: tuple[dict, ...] = ()
    # Public policy explanation is not a SKU price, payment mode or refund.
    approved_payment_policy: tuple[tuple[str, str, str], ...] = ()
    # Exact reviewed general made-to-order prose; no order/payment/stock truth.
    approved_fulfillment_policy: tuple[tuple[str, str, str], ...] = ()
    # Internal derived metadata; kept last for constructor compatibility.
    source_price_subjects: tuple[SourcePriceSubject, ...] = ()


@dataclass(frozen=True)
class ReplyTruthResult:
    valid: bool
    reasons: tuple[str, ...] = ()


_URL_RE = re.compile(r"https?://[^\s<>]+", re.I)
_AGREEMENT_ATTRIBUTION_RE = re.compile(
    r"домов\w*|узгод\w*|погод\w*|соглас\w*|договор\w*|\bagreed\b|\bagreement\b|as\s+quoted", re.I)
_SELLER_QUOTE_ATTRIBUTION_RE = re.compile(
    r"за\s+розрахун\w*\s+менеджер\w*|по\s+расч[её]т\w*\s+менеджер\w*|"
    r"менеджер\w*\s+(?:порах\w*|розрах\w*|указ\w*|напис\w*)|"
    r"(?:manager|seller)\s+(?:quoted|calculated)|as\s+quoted", re.I)
_AGREEMENT_COMPONENT_RE = re.compile(
    r"(?P<payable>разом|всього|итого|загалом|total|до\s+сплати|к\s+оплате|payable)|"
    r"(?P<delivery>достав\w*|shipping|delivery)|"
    r"(?P<merchandise>футбол\w*|худі|худи|товар\w*|t[- ]?shirt\w*|hoodie\w*|merchandise|goods|product\w*|item\w*)", re.I)


def _agreed_amount_allowed(sentence, match, context):
    agreed = bool(_AGREEMENT_ATTRIBUTION_RE.search(sentence))
    quoted = bool(_SELLER_QUOTE_ATTRIBUTION_RE.search(sentence))
    if not agreed and not quoted:
        return False
    prefix = sentence[:match.start()]
    terms = list(_AGREEMENT_COMPONENT_RE.finditer(prefix))
    kind = terms[-1].lastgroup if terms else "payable"
    if re.search(r"=\s*$", prefix):
        kind = "payable"
    for row in context.conversation_amounts[:80]:
        if (not isinstance(row, (tuple, list)) or len(row) != 5
                or row[0] != kind or row[2] != _currency_code(match.group("currency"))
                or type(row[3]) is not int or row[3] <= 0
                or row[4] not in {"conversation_agreement", "seller_instruction"}
                or row[4] == "seller_instruction" and not quoted):
            continue
        if _decimal(row[1]) == _decimal(match.group("amount")):
            return True
    return False
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
# An open vacancy never authorizes accepting a particular applicant. There is
# deliberately no hiring-decision authority in ReplyTruthContext yet.
_RECRUITMENT_COMMITMENT_RE = re.compile(
    r"\b(?:(?:вас|ви|вы)\s+(?:(?:вже|уже)\s+)?(?:прийнят\w*|принят\w*)\s+(?:на\s+робот\w*|на\s+работ\w*|[ув]\s+команд\w*|до\s+команд\w*)"
    r"|(?:беремо|беру|берем|берём|приймаємо|приймаю|принимаем|принимаю)\s+вас\s+(?:[ув]|до|на)\s+(?:команд\w*|робот\w*|работ\w*)"
    r"|(?:наймаємо|нанимаем)\s+вас"
    r"|(?:ви|вы)\s+(?:(?:вже|уже|тепер|теперь)\s+)?наш\w*\s+(?:нов\w*\s+)?(?:менеджер\w*|працівник\w*|співробітник\w*|сотрудник\w*)"
    r"|you(?:\s+are|['’]re|\s+have\s+been|['’]ve\s+been)\s+(?:(?:now|officially)\s+)?hired"
    r"|(?:ваш\w*\s+)?кандидатур\w*\s+(?:(?:вже|уже|була|была)\s+)?(?:схвален\w*|затверджен\w*|одобрен\w*|утвержден\w*)"
    r"|you(?:\s+are|['’]re|\s+have\s+been|['’]ve\s+been)\s+(?:(?:now|officially)\s+)?accepted\s+(?:for|into|to)\s+(?:the\s+|our\s+)?(?:role|position|job|team)"
    r"|your\s+candidacy\s+(?:(?:is|was|has\s+been)\s+)?(?:approved|accepted)"
    r"|we(?:\s+are|['’]re)\s+hiring\s+you|we\s+(?:have\s+)?hired\s+you)\b", re.I,
)
_RECRUITMENT_HYPOTHETICAL_RE = re.compile(
    r"\b(?:якщо|якби|если|if|whether|припуст\w*|уяв\w*|предполож\w*|suppose|imagine)\b"
    r"|(?:не\s+(?:ствердж\w*|каж\w*|підтвердж\w*)|(?:cannot|can['’]t)\s+(?:say|confirm))[^;.!?]{0,80}$", re.I,
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
_FULL_PAYMENT_RE = re.compile(r"повніст\w*\s+(?:оплач\w*|сплач\w*)|полностью\s+оплач\w*|fully\s+paid|paid\s+in\s+full|full\s+payment[^,;.!?]{0,20}(?:confirmed|received)|(?:замовлен\w*|заказ\w*|order)[^,;.!?]{0,25}\b(?:paid|оплачен\w*|оплачен\w*|оплачено)\b", re.I)
_PARTIAL_PAYMENT_RE = re.compile(r"передоплат\w*|предоплат\w*|частков\w*|частич\w*|\b(?:deposit|prepayment|prepaid|partially)\b", re.I)
_EXPLICIT_FULL_RE = re.compile(r"повніст\w*|полностью|fully|in\s+full|full\s+payment", re.I)
_CHECKOUT_COMPONENT_RE = re.compile(
    r"(?P<balance>залиш\w*[^,;.!?]{0,25}(?:сплат\w*|оплат\w*)|оста[тл]\w*[^,;.!?]{0,25}(?:оплат\w*|доплат\w*)|доплат\w*|remaining|balance|still\s+due|cash\s+on\s+delivery)|"
    r"(?P<paid>оплатили|сплатили|внес\w*\s+(?:передоплат\w*|предоплат\w*)|отримали\s+оплату|получили\s+оплату|paid|deposit|prepayment|received\s+payment)|"
    r"(?P<gross>до\s+зниж\w*|до\s+скид\w*|before\s+discounts?|gross|subtotal)|"
    r"(?P<merchandise>після\s+зниж\w*|после\s+скид\w*|after\s+discounts?|merchandise)|"
    r"(?P<discount>зниж\w*|скид\w*|discount)|"
    r"(?P<payable>разом|всього|итого|до\s+сплати|к\s+оплате|payable|total)", re.I)
_SHIPPING_CONTEXT_RE = re.compile(r"достав\w*|перевізник\w*|перевозчик\w*|\b(?:shipping|delivery|carrier)\b", re.I)
_SHIPPING_FREE_RE = re.compile(r"безкоштов\w*|бесплат\w*|\bfree\b", re.I)
_SHIPPING_MERCHANT_RE = re.compile(r"(?:twocomms|магазин\w*|\bstore\b|\bmerchant\b|\bwe\b|\bми\b|\bмы\b)[^,;.!?]{0,35}(?:опла\w*|спла\w*|плат\w*|\bpay\w*|\bcover\w*)|(?:paid|covered)\s+by\s+(?:twocomms|the\s+store|us)|(?:опла\w*|спла\w*)[^,;.!?]{0,20}(?:магазин\w*|twocomms)", re.I)
_SHIPPING_RECIPIENT_RE = re.compile(r"(?:одержувач\w*|отримувач\w*|получател\w*|\byou\b|\bcustomer\b|\brecipient\b|\bви\b|\bвы\b)[^,;.!?]{0,25}(?:опла\w*|спла\w*|плат\w*|\bpay\w*)|(?:paid|опла\w*|спла\w*)[^,;.!?]{0,30}(?:окремо|отдельно|separately)", re.I)
_SHIPPING_INCLUDED_RE = re.compile(r"включ\w*|вход\w*\s+(?:у|в)\s+(?:сум\w*|оплат\w*)|\b(?:included|prepaid)\b|передоплач\w*|предоплач\w*", re.I)
_SHIPPING_THRESHOLD_RE = re.compile(r"(?:\bвід|\bот|\bfrom|at\s+least|threshold|поріг|порог)\s*(?:суми\s+|суммы\s+|\w{3}\s+)?$", re.I)
_SHIPPING_CAP_RE = re.compile(r"\bдо\b|up\s+to|максим\w*|maximum|\bcap\b|at\s+most", re.I)
_SHIPPING_ACTUAL_RE = re.compile(r"фактич\w*|фактичес\w*|\bactual\b|тариф\w*|\b(?:tariff|quote|estimated)\b|кошту\w*|\bстоит\b|\bcosts?\b|carrier[^,;.!?]{0,15}charges?", re.I)
_SHIPPING_PHASES = {"now": re.compile(r"зараз|сейчас|\bnow\b|готов\w*\s+(?:част\w*|позиц\w*|товар\w*)|ready\s+(?:part|items)", re.I),
    "later": re.compile(r"пізніш\w*|позже|решт\w*|залишк\w*|оставш\w*|\b(?:later|remaining)\b", re.I),
    "together": re.compile(r"разом|вместе|\btogether\b", re.I)}


def _shipping_facts(sentence, context):
    phases = [phase for phase, pattern in _SHIPPING_PHASES.items() if pattern.search(sentence)]
    if len(phases) > 1:
        return ()
    facts = tuple(fact for fact in context.shipping_authorities if isinstance(fact, ShippingAuthority) and fact.source_ref)
    if phases:
        return tuple(fact for fact in facts if fact.phase == phases[0] or not fact.phase and phases[0] != "later")
    if context.checkout_authority is not None:
        return facts
    return tuple(fact for fact in facts if not fact.phase)


def _shipping_positive(pattern, sentence):
    for match in pattern.finditer(sentence):
        if _locally_negated(sentence, match.start()):
            continue
        if re.search(r"\b(?:не|not|don['’]?t|doesn['’]?t|won['’]?t)\b[^,;.!?]{0,15}(?:опла\w*|спла\w*|плат\w*|pay|cover|free|included)", match.group(), re.I):
            continue
        return True
    return False


def _shipping_money_allowed(sentence, match, context):
    component = _SHIPPING_CONTEXT_RE.search(sentence)
    if component is None:
        return None
    prefix = sentence[:match.start()]
    threshold = bool(_SHIPPING_THRESHOLD_RE.search(prefix) and _shipping_positive(_SHIPPING_FREE_RE, sentence))
    if match.end() <= component.start() and not threshold:
        return None
    # A subsequent explicitly named goods component has its own money domain.
    after = prefix[component.end():]
    if re.search(r"(?:ціна|цена|\bprice\b|\bgoods\b|товар\w*)[^,;.!?]*$", after, re.I):
        return None
    amount, currency = _decimal(match.group("amount")), _currency_code(match.group("currency"))
    facts = _shipping_facts(sentence, context)
    if threshold:
        return any(fact.authority == "automatic_threshold" and fact.policy_version == "threshold-after-discounts.v1"
            and fact.threshold == amount and fact.currency == currency for fact in facts)
    if _shipping_positive(_SHIPPING_ACTUAL_RE, sentence):
        return False
    if not facts and context.checkout_authority is None and _agreed_amount_allowed(sentence, match, context):
        return True  # Attributed seller arrangement, never an actual carrier bill.
    if _SHIPPING_CAP_RE.search(prefix.split(";")[-1]):
        return any(fact.customer_maximum == amount and fact.currency == currency for fact in facts)
    customer_amount = bool(re.search(r"для\s+вас|for\s+you|you\s+pay|ви\s+плат|вы\s+плат|included|включ", prefix, re.I))
    return bool(customer_amount and any(fact.customer_charge == amount and fact.currency == currency for fact in facts))


def _checkout_money_allowed(sentence, match, context):
    fact = context.checkout_authority
    if fact is None:
        return None
    prefix = sentence[:match.start()]
    labels = list(_CHECKOUT_COMPONENT_RE.finditer(prefix))
    named = [item for item in fact.items if re.search(re.escape(item.title), prefix, re.I)]
    if named and re.search(r"(?:ціна|цена|price)\s*[:—–-]?\s*$", prefix, re.I):
        return any(_decimal(match.group("amount")) in {item.unit_price, item.line_total} for item in named)
    if not labels:
        return None
    kind = labels[-1].lastgroup
    value = {"paid": fact.confirmed_paid, "balance": fact.balance, "gross": fact.gross,
        "merchandise": fact.merchandise_payable, "payable": fact.payable, "discount": fact.discount}[kind]
    return value is not None and value == _decimal(match.group("amount")) and _currency_code(match.group("currency")) == "UAH"


def _shipping_claim_unit_verified(sentence, context):
    if not _SHIPPING_CONTEXT_RE.search(sentence):
        return True
    facts = _shipping_facts(sentence, context)
    free = _shipping_positive(_SHIPPING_FREE_RE, sentence)
    threshold_rule = any(_SHIPPING_THRESHOLD_RE.search(sentence[:match.start()])
        and _shipping_money_allowed(sentence, match, context) is True
        for pattern in (_MONEY_RE, _PREFIX_MONEY_RE) for match in pattern.finditer(sentence))
    if free and not threshold_rule and not any(fact.mode == "merchant_free" and fact.payer_type == "Sender" for fact in facts):
        return False
    if _shipping_positive(_SHIPPING_MERCHANT_RE, sentence) and not any(fact.payer_type == "Sender" for fact in facts):
        return False
    if _shipping_positive(_SHIPPING_INCLUDED_RE, sentence) and not any(fact.delivery_prepaid and fact.mode in {"customer_prepaid", "customer_paid_included"} for fact in facts):
        return False
    if _shipping_positive(_SHIPPING_RECIPIENT_RE, sentence):
        zero = any(_decimal(match.group("amount")) == 0 and _shipping_money_allowed(sentence, match, context) is True
            for pattern in (_MONEY_RE, _PREFIX_MONEY_RE) for match in pattern.finditer(sentence))
        if not zero and not any(fact.payer_type == "Recipient" for fact in facts):
            return False
    return True


def _shipping_claims_verified(sentence, context):
    shipping_seen = False
    for clause in re.split(r"(?<!\d),(?!\d)|;", sentence):
        explicit = bool(_SHIPPING_CONTEXT_RE.search(clause))
        shipping_seen = shipping_seen or explicit
        if not explicit and shipping_seen and any(pattern.search(clause) for pattern in (
                _SHIPPING_FREE_RE, _SHIPPING_MERCHANT_RE, _SHIPPING_RECIPIENT_RE, _SHIPPING_INCLUDED_RE)):
            clause = "delivery " + clause
        if not _shipping_claim_unit_verified(clause, context):
            return False
    return True


def _checkout_basket_verified(sentence, context):
    fact = context.checkout_authority
    if fact is None:
        return True
    for clause in re.split(r"[,;]|\s+(?:and|та|и)\s+", sentence, flags=re.I):
        for item in fact.items:
            if not re.search(re.escape(item.title), clause, re.I):
                continue
            quantities = [int(first or second) for first, second in _STOCK_QUANTITY_RE.findall(clause)]
            quantities.extend(int(match) for match in re.findall(r"[×x]\s*(\d{1,3})\b", clause, re.I))
            if quantities and any(value != sum(row.quantity for row in fact.items if row.title == item.title) for value in quantities):
                return False
    if re.search(r"paid\s+for|your\s+order|у\s+замовлен\w*|в\s+заказ\w*|оплатили|сплатили", sentence, re.I):
        current_titles = {item.title for item in fact.items}
        for change in fact.as_dict().get("checkout_changes", []):
            if change.get("operation") != "remove":
                continue
            after = {row.get("title") for row in change.get("after", [])}
            for row in change.get("before", []):
                title = row.get("title")
                if title and title not in after and title not in current_titles:
                    for match in re.finditer(re.escape(title), sentence, re.I):
                        if not _locally_negated(sentence, match.start()) and not re.search(r"removed|вилуч\w*|видал\w*|удален\w*|убран\w*", sentence, re.I):
                            return False
    return True
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
    r"production|deliver\w*|ship\w*|dispatch\w*|"
    r"отрима\w*|получи\w*|приб\w*|приед\w*|arriv\w*|receive\w*",
    re.I,
)
_TIMING_RE = re.compile(
    r"\b(?:"
    r"(?:до\s+|within\s+|in\s+)?\d{1,3}(?:\s*[-–—]\s*\d{1,3})?\s*"
    r"(?:(?:робоч\w*|рабоч\w*|business)\s+)?"
    r"(?:хвилин\w*|минут\w*|minutes?|годин\w*|час\w*|hours?|"
    r"дн(?:і|ів|я)|день|дня|дней|days?|тижн\w*|недел\w*|weeks?)"
    r"|(?:сьогодні|сегодня|завтра|післязавтра|послезавтра|today|tomorrow|same[- ]day)"
    r"|(?:до|к|by|on)\s+(?:\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?"
    r"|\d{1,2}\s+[^\W\d_]{3,16}|\d{1,2}(?:-го)?(?=\s*(?:числ\w*|[.!?]?$)))"
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
    r"достав\w*|deliver\w*|перевізник\w*|перевозчик\w*|carrier\w*|transit|"
    r"arrival|arriv\w*|прибут\w*|нова\s+пошта|нов\w*\s+почт\w*|nova\s+poshta",
    re.I,
)
_BASE_ARRIVAL_RE = re.compile(
    r"(?:прибут\w*|прибуд\w*|приїд\w*|прийд\w*|прид\w*|надход\w*|надійд\w*|поступлен\w*|поступит\w*)\s+(?:цієї\s+|этой\s+)?основ\w*|"
    r"основ\w*\s+(?:прибуд\w*|приїд\w*|прийд\w*|прид\w*|надійд\w*|поступит\w*|очікує\w*|ожидает\w*)|"
    r"\b(?:bases?|blanks?)\s+(?:arrival|arrives?|is\s+arriving)|\barrival\s+of\s+(?:the\s+)?(?:bases?|blanks?)\b", re.I,
)
_UNPARSED_BASE_DURATION_RE = re.compile(
    r"\bnext\s+week\b|\b(?:через|за|within|in)\s+(?:сім\w*|семь|seven|тиждень|недел\w*|a\s+week)\b", re.I,
)
_PREPARATION_RE = re.compile(
    r"підготов\w*|подготов\w*|виготов\w*|изготов\w*|вироб\w*|"
    r"готу\w*|готов\w*|prepar\w*|production|manufactur\w*|\bready\b", re.I,
)
_DISPATCH_RE = re.compile(r"відправ\w*|отправ\w*|dispatch\w*|ship\w*", re.I)
_ARRIVAL_RE = re.compile(
    r"отрима\w*|получи\w*|прибут\w*|прибуд\w*|приед\w*|"
    r"забере\w*|забра\w*|приде\w*|arriv\w*|receive\w*|reach\w*|pick\s*up|"
    r"(?:буде|будет|will\s+be)\s+(?:у\s+вас|в\s+вас|with\s+you)", re.I,
)
_DEADLINE_FEASIBILITY_RE = re.compile(
    r"\b(?:встигнемо|встигне\w*|успеем|успеет\w*|"
    r"we(?:\s+will|['’]ll)\s+make\s+it|(?:will|can)\s+arrive\s+in\s+time|"
    r"(?:will|can)\s+meet\s+(?:the\s+|your\s+)?deadline|"
    r"(?:точно|гарантовано|гарантированно|definitely)\s+(?:вчасно|вовремя|on\s+time))\b", re.I,
)
_TIMING_CLAUSE_BOUNDARY_RE = re.compile(
    r";|\s+(?:але|однак|но|однако|but|however)\s+", re.I,
)
_TIMING_HISTORY_RE = re.compile(
    r"\b(?:раніше|раньше|ранее|previously|last\s+time|у\s+минул\w*|"
    r"минулого\s+разу|в\s+прошл\w*)\b[^.!?;]{0,100}"
    r"(?:каз\w*|сказ\w*|говор\w*|пис\w*|обіц\w*|обещ\w*|тривал\w*|"
    r"занима\w*|said|told|wrote|promised|took)", re.I,
)
_TIMING_UNCERTAINTY_RE = re.compile(
    r"(?:не\s+(?:(?:можемо|можу|можем|могу)\s+)?"
    r"(?:гаранту\w*|гарантир\w*|обіця\w*|обещ\w*|встига\w*|встигнемо|успеем)|"
    r"(?:cannot|can['’]?t|do\s+not|don['’]?t)\s+"
    r"(?:guarantee|promise|confirm|make\s+it|meet\s+(?:the\s+)?deadline)|"
    r"без\s+гаранті\w*|без\s+гаранти\w*|not\s+guaranteed)", re.I,
)
_CARRIER_ESTIMATE_QUALIFIER_RE = re.compile(
    r"орієнтов\w*|ориентиров\w*|приблиз\w*|примерн\w*|"
    r"estimate\w*|approximately|approx\.?|usually|typically|зазвичай|обычно", re.I,
)
_NUMERIC_DEADLINE_RE = re.compile(
    r"\b(?:до|к|by|on)\s+(?:\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?"
    r"|\d{1,2}\s+[^\W\d_]{3,16}|\d{1,2}(?:-го)?(?=\s*(?:числ\w*|[.!?]?$)))\b", re.I,
)
_TIMING_START_RE = re.compile(
    r"\b(?:starting|starts?|beginning|begins?|counting|from|since|"
    r"почина\w*|почне\w*|начина\w*|начне\w*|рахуючи|отсчитывая|від|з|с|от)"
    r"\s+(?:(?:on|from|з|від|с|от)\s+)?(?P<date>"
    r"\d{4}-\d{2}-\d{2}|\d{1,2}[.-]\d{1,2}(?:[.-]\d{2,4})?|"
    r"\d{1,2}(?:(?:st|nd|rd|th)|-го)?\s+[^\W\d_]{3,16}(?:\s*,?\s*\d{4})?|"
    r"[^\W\d_]{3,16}\s+\d{1,2}(?:st|nd|rd|th)?(?:\s*,?\s*\d{4})?|"
    r"сьогодні|сегодня|завтра|today|tomorrow|\d{1,2}(?:-го)?)\b", re.I,
)
_DEADLINE_PROMISE_RE = re.compile(
    r"\b(?:буде|будет|будут|зробимо|сделаем|will\s+be|will\s+have)\b", re.I,
)
_TIMING_ENQUIRY_RE = re.compile(
    r"(?:уточн\w*|перевір\w*|провер\w*|ask\w*|check\w*|confirm)"
    r"[^.!?;]{0,60}(?:\bчи\b|\bли\b|\bwhether\b|\bif\b)", re.I,
)
_SELECTION_WORDS = (
    r"(?:обран(?:ий|а|е|і)|вибран(?:ий|а|е|і)|"
    r"выбран(?:ный|ная|ное|ные)|selected|chosen)"
)
_SIZE_RE = re.compile(
    rf"\b(?:(?P<selected>{_SELECTION_WORDS})\s+)?"
    r"(?:розмір|размер|size|fit\s+in(?:\s+size)?)(?:\s+requirement)?(?:\s+is)?\s*(?P<separator>[:=–—-])?\s*[\"'«“]?"
    r"(?P<value>[A-Za-z0-9_-]{1,16})\b",
    re.I,
)
_FIT_RE = re.compile(
    r"\b(?:(?P<descriptive_fit>regular|classic|oversize|slim|relaxed|loose|fitted|standard)\s+fit\b(?!\s*[:=-])|"
    rf"(?:(?P<selected>{_SELECTION_WORDS})\s+)?"
    r"(?:крій|крой|fit)\s*(?P<separator>[:=-])?\s*"
    r"(?P<value>[A-Za-z0-9_-]{1,32})\b)",
    re.I,
)
_COLOR_RE = re.compile(
    rf"\b(?:(?P<selected>{_SELECTION_WORDS})\s+)?"
    r"(?:колір|цвет|color)\s*(?P<separator>[:=-])?\s*"
    r"(?P<value>[\w-]{2,40})\b",
    re.I,
)
_CUSTOMER_CHOICE_RE = re.compile(
    r"(?:ви|вы|you)\s+(?:обрали|вибрали|выбрали|chose|have\s+chosen|selected)"
    r"|(?:ваш\w*|your)\s+(?:вибір|выбор|choice|побажан\w*|пожелан\w*|preference)"
    r"|(?:враху\w*|учту|зафікс\w*|запис\w*|сохран\w*|noted|recorded|requested|preference)", re.I,
)
_AVAILABILITY_RE = re.compile(
    r"(?:\bє\b|\bесть\b|\bмаємо\b|\bимеем\b|\bесть\b|we\s+have(?!\s+(?:chosen|selected|requested))|\bhave\b(?!\s+(?:chosen|selected|requested))|\bдоступн\w*|\bнаявн\w*|\bavailable\b|in\s+stock)\s+[^.!?\n]{0,30}"
    r"(?:розмір|размер|size|\b(?:XS|S|M|L|XL|XXL|XXXL)\b)"
    r"|(?:розмір|размер|size|\b(?:XS|S|M|L|XL|XXL|XXXL)\b)[^.!?\n]{0,35}"
    r"(?:\bє\b|\bесть\b|\bмаємо\b|\bимеем\b|we\s+have(?!\s+(?:chosen|selected|requested))|\bhave\b(?!\s+(?:chosen|selected|requested))|\bдоступн\w*|\bнаявн\w*|\bavailable\b|in\s+stock)", re.I,
)
# Nominal stock assurances are physical claims even without "is available"
# or a nearby size token. Keep the noun, garment scope and explicit assertion
# inside one bounded clause; product-title casing grants no authority.
_NOMINAL_STOCK_NOUN = r"(?:наявн(?:ість|ості)|налич(?:ие|ия|ию)|доступн(?:ість|ості|ость|ости)|availability)"
_NOMINAL_STOCK_ASSURANCE = (r"(?:стовідсотков\w*|стопроцентн\w*|повн(?:а|е|ий|і|істю)|полн(?:ое|ая|ый|ые|остью)|"
    r"гарантован\w*|гарантирован\w*|забезпечен\w*|обеспечен\w*|certain|assured|guaranteed|full|100\s*%)")
_NOMINAL_STOCK_RE = re.compile(
    rf"\b{_NOMINAL_STOCK_NOUN}\b[^,;.!?\n]{{0,100}}?(?<!\w)(?P<after>{_NOMINAL_STOCK_ASSURANCE})(?!\w)|"
    rf"(?<!\w)(?P<before>{_NOMINAL_STOCK_ASSURANCE})[^,;.!?\n]{{0,100}}?\b{_NOMINAL_STOCK_NOUN}\b"
    r"[^,;.!?\n]{0,60}", re.I,
)
_CONFIGURATION_STOCK_RE = re.compile(
    r"\b(?:маємо|имеем|доступн\w*|наявн\w*|available)\b|in\s+stock|"
    r"\b(?:є|есть)\b|\bhave\b(?!\s+(?:chosen|selected|requested))", re.I,
)
_EXACT_STOCK_RE = re.compile(
    r"\b(?:у|в)\s+(?:наявності|наличии)\b|\bнаявн(?:ий|а|е|і)\b|in\s+stock|"
    r"(?:підтвердив|підтвердила|підтверджено|подтвердил|подтвердила|подтверждено)"
    r"[^,;.!?]{0,60}\b(?:наявність|наявності|наличие|наличия)\b|"
    r"\b(?:products?|items?|hoodies?|t[- ]?shirts?)\b[^,;.!?]{0,60}\bavailable\b|"
    r"\bavailable\b[^,;.!?]{0,60}\b(?:products?|items?|hoodies?|t[- ]?shirts?)\b", re.I,
)
_PHOTO_EXISTENCE_RE = re.compile(
    r"\b(?:є|есть|маємо|имеем|we\s+have|have)\s+"
    r"(?:(?:окрем\w*|отдельн\w*|підтверджен\w*|подтвержд[её]н\w*|confirmed|separate|a|an|some)\s+){0,3}"
    r"(?:фото\w*|зображен\w*|изображен\w*|photos?|pictures?|images?)\b", re.I,
)
_STOCK_NONASSERTION_RE = re.compile(
    r"\b(?:якщо|если|if|when|чи|ли|whether)\b[^,;.!?]*$|"
    r"\b(?:після|после|after)\b[^,;.!?]{0,70}"
    r"(?:підтвердж\w*|подтвержд\w*|confirm\w*|перевір\w*|провер\w*|check\w*)[^,;.!?]*$|"
    r"\b(?:уточню|уточнимо|уточним|уточнить|перевірю|перевіримо|перевірити|"
    r"проверю|проверим|проверить|check|checking|verify|verifying)\b[^,;.!?]*$|"
    r"\bне\s+(?:(?:можу|можемо|могу|можем)\s+)?(?:підтверд\w*|підтвердж\w*|подтверд\w*|подтвержд\w*)\b[^,;.!?]*$|"
    r"\b(?:cannot|can['’]?t|do\s+not|don['’]?t)\s+confirm\b[^,;.!?]*$", re.I,
)
_WHOLE_CART_STOCK_RE = re.compile(
    r"усі\s+(?:обрані\s+|замовлені\s+)?(?:позиції|товари)|"
    r"все\s+(?:выбранные\s+|заказанные\s+)?(?:позиции|товары)|"
    r"all\s+(?:selected\s+|requested\s+|ordered\s+)?(?:items|lines)|"
    r"(?:ціле|все)\s+замовлення|весь\s+заказ|whole\s+(?:order|cart)", re.I,
)
_STOCK_QUANTITY_RE = re.compile(
    r"(?<!\d)(\d{1,3})\s*(?:шт\.?|одиниц\w*|единиц\w*|pieces?|units?|items?|"
    r"футболк\w*|худі|худи|(?:t[- ]?)?shirts?|hoodies?)\b|[×x]\s*(\d{1,3})(?!\d)", re.I,
)
_STOCK_CLAUSE_BOUNDARY_RE = re.compile(
    r";|\s+(?:але|но|but|however|and\s+(?:we|it|they)|і\s+ми|и\s+мы)\s+", re.I,
)
_STOCK_SUBJECT_RE = re.compile(
    r"\b(?:товар\w*|продукт\w*|модел\w*|позиці\w*|позици\w*|футболк\w*|худі|худи|"
    r"розмір\w*|размер\w*|size\w*|products?|items?|models?|hoodies?|t[- ]?shirts?|XS|S|M|L|XL|XXL|XXXL)\b", re.I,
)
_BARE_PRODUCT_STOCK_RE = re.compile(
    r"\b(?:товар\w*|продукт\w*|модел\w*|футболк\w*|худі|худи|products?|items?|models?|hoodies?|t[- ]?shirts?)\b"
    r"[^,;.!?\n]{0,60}\b(?:є|есть|доступн\w*|available)\b|"
    r"\b(?:є|есть|доступн\w*|available)\b[^,;.!?\n]{0,60}"
    r"\b(?:товар\w*|продукт\w*|модел\w*|футболк\w*|худі|худи|products?|items?|models?|hoodies?|t[- ]?shirts?)\b", re.I,
)
_CATALOG_EXISTENCE_RE = re.compile(
    r"\b(?:[ув]\s+каталозі|в\s+каталоге|in\s+(?:our\s+|the\s+)?catalog(?:ue)?)\s+(?:є|есть|are|is|available)\b|"
    r"\b(?:є|есть|available)\s+(?:[ув]\s+каталозі|в\s+каталоге|in\s+(?:our\s+|the\s+)?catalog(?:ue)?)\b", re.I,
)
_NEGATIVE_STOCK_RE = re.compile(
    r"\bout\s+of\s+stock\b|\bsold\s+out\b|\bunavailable\b|\b(?:not|isn['’]?t)\s+(?:currently\s+)?(?:available|in\s+stock)\b|"
    r"\b(?:немає|нема|відсутн\w*|отсутств\w*|недоступн\w*)\b|\bнет\b|\bне\s+(?:доступн\w*|наявн\w*|[ву]\s+(?:наявності|наличии))\b", re.I,
)
_PHOTO_ABSENCE_RE = re.compile(
    r"\b(?:немає|нема|нет|відсутн\w*|отсутств\w*|no)\s+(?:(?:окрем\w*|отдельн\w*|confirmed|separate)\s+){0,2}"
    r"(?:фото\w*|зображен\w*|изображен\w*|photos?|pictures?|images?)\b|"
    r"\b(?:фото\w*|зображен\w*|изображен\w*|photos?|pictures?|images?)\s+(?:are\s+|is\s+)?(?:unavailable|недоступн\w*|відсутн\w*)\b", re.I,
)
_NONPHYSICAL_ABSENCE_RE = re.compile(
    r"\s+(?:(?:підтверджен\w*|подтвержд[её]н\w*|точн\w*|confirmed|exact)\s+){0,2}"
    r"(?:замір\w*|вимір\w*|измер\w*|заміров\w*|ваг\w*|вес\w*|даних|данных|інформаці\w*|информаци\w*|measurements?|weight|data|information)\b", re.I,
)
_WAIT_STOCK_RE = re.compile(
    r"\b(?:під|под)\s+замовлен\w*|\bпод\s+заказ\b|\bmade[- ]to[- ]order\b|\bon\s+backorder\b|"
    r"\b(?:очікуємо|очікує\w*|чекати|ждать|ожидаем|ожидает\w*|awaiting|waiting\s+for)\b"
    r"[^,;.!?\n]{0,60}(?:основ\w*|надход\w*|поступлен\w*|товар\w*|футболк\w*|худі|худи|stock|supply|base|blank|hoodie|shirt)", re.I,
)

_NON_CONFIGURATION_VALUES = frozenset({
    "depends", "залежить", "зависит", "впливає", "влияет", "varies",
    "chart", "таблиця", "таблица", "на", "on",
})
_AVAILABILITY_CHECK_RE = re.compile(
    r"\b(?:уточню|уточнимо|перевірю|перевіримо|проверю|проверим|check|checking|verify|verifying)\b|"
    r"\b(?:cannot|can['’]?t|do\s+not|don['’]?t)\s+confirm\b|"
    r"\bне\s+(?:(?:можу|можемо|могу|можем)\s+)?(?:підтверд\w*|підтвердж\w*|подтверд\w*|подтвержд\w*)\b|"
    r"\b(?:needs?|requires?)\s+(?:manager\s+)?(?:verification|checking|to\s+be\s+checked)\b", re.I,
)
_AVAILABILITY_CHECK_SUBJECT_RE = re.compile(
    r"наявн\w*|налич\w*|доступн\w*|availability|available|in\s+stock", re.I,
)
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
    # URLs have their own exact allowlist check. Their path/query tokens are
    # identifiers, not prose claims (for example size_grids is not a size).
    # Mask before sentence splitting so dots inside a URL cannot expose a
    # detached path fragment to the configuration/price/availability parsers.
    claims = _URL_RE.sub(lambda match: " " * len(match.group(0)), claims)
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
        for match in _RECRUITMENT_COMMITMENT_RE.finditer(clause):
            prefix = clause[:match.start()]
            if not (_locally_negated(clause, match.start())
                    or _RECRUITMENT_UNCERTAINTY_RE.search(prefix)
                    or _RECRUITMENT_HYPOTHETICAL_RE.search(prefix.rsplit(",", 1)[-1])):
                yield "hired"
        for match in _RECRUITMENT_STATUS_RE.finditer(clause):
            if not _RECRUITMENT_UNCERTAINTY_RE.search(clause[:match.start()]):
                yield match.lastgroup


def _add(reasons: list[str], reason: str) -> None:
    if reason in REASON_CODES and reason not in reasons:
        reasons.append(reason)


def _positive_configuration_value(sentence: str, match: re.Match) -> str:
    """Return an asserted selection, excluding descriptive label phrases."""
    if match.re is _FIT_RE and match.group("descriptive_fit"):
        # In a noun phrase the adjective names the fit, independently of the
        # conjunction, preposition or noun following it. Explicit fit labels
        # and separators remain on the value branch and retain their checks.
        return match.group("descriptive_fit")
    claimed = str(match.group("value") or "")
    if _normalize(claimed) in _NON_CONFIGURATION_VALUES:
        return ""
    if match.group("selected") or match.group("separator"):
        return claimed
    if _normalize(claimed) in {"for", "with", "of"}:
        # A noun followed by a preposition is descriptive grammar, not an
        # option named "for". Preserve a real pre-nominal fit assertion:
        # "oversize fit for..." still has to match the captured authority.
        if match.re is _FIT_RE:
            preceding_fit = re.search(
                r"\b(regular|classic|oversize|slim|relaxed|loose|fitted|standard)\s+$",
                sentence[:match.start()], re.I,
            )
            if preceding_fit:
                return preceding_fit.group(1)
        return ""
    local_tail = re.split(r"[,;.!?]", sentence[match.end():], maxsplit=1)[0]
    if _CONFIGURATION_DESCRIPTION_RE.search(local_tail[:120]):
        return ""
    return claimed


def _source_price_subject_matches(sentence, context):
    """Exact title/configuration grammar; an unknown named subject fails closed."""
    from management.services.ig_response_cart_plan import single_price_subject
    for subject in context.source_price_subjects:
        if (type(subject) is not SourcePriceSubject or not subject.title or not subject.line_id
            or type(subject.product_id) is not int or subject.product_id <= 0
            or not re.fullmatch(r"[a-f0-9]{64}", subject.capture_digest)
            or not subject.source_message_ids or any(type(value) is not int or value <= 0 for value in subject.source_message_ids)
            or not re.search(r"(?<!\w)" + re.escape(subject.title) + r"(?!\w)", sentence, re.I)):
            continue
        choices = dict(subject.choices)
        mentions = [*(_MONEY_RE.finditer(sentence)), *(_PREFIX_MONEY_RE.finditer(sentence))]
        if (len(mentions) != 1 or _decimal(mentions[0].group("amount")) not in subject.prices
            or _decimal(mentions[0].group("amount")) not in context.authorized_prices):
            continue
        value = sentence
        fit = choices.get("fit_option_code", "")
        fit_words = (r"classic|regular|класич\w*|классич\w*" if fit == "classic"
            else r"oversize|оверсайз\w*" if fit == "oversize" else re.escape(fit) if fit else r"(?!)")
        value = re.sub(r"(?<!\w)(?:" + fit_words + r")\s+(?:крій|крой|fit)(?!\w)", " ", value, flags=re.I)
        value = _FIT_RE.sub(lambda match: " " if _configuration_axis_value("fit", _positive_configuration_value(value, match))
            == _configuration_axis_value("fit", fit) and fit else match.group(), value)
        readiness = {"product": {"id": subject.product_id, "kind": subject.product_kind},
            "fit": {"selected": fit}}
        if single_price_subject(value, choices=choices, title=subject.title, readiness=readiness):
            return True
    return False


def _configuration_axis_value(axis: str, value) -> str:
    if axis == "size":
        from product_catalog.size_grid_services import normalize_size_value
        # Unknown/numeric values retain their literal identity. They never
        # collapse to an empty token that could match an unrelated size.
        return _normalize(normalize_size_value(value))
    if axis == "fit":
        # The native catalogue code "classic" and its English description
        # "regular" identify the same cut; oversize remains a separate value.
        return {"regular": "classic"}.get(_normalize(value), _normalize(value))
    return _normalize(value)


def _comma_choice_continuation(sentence, clause_start, clause_end):
    """Carry a wish only through bare comma-separated configuration items.

    Source wishes authorize neither another speaker's statement nor stock.
    Existing axis parsers identify list items; leftover subjects/predicates,
    hard clause boundaries and negated/reported introductions end this scope.
    """
    if not clause_start or sentence[clause_start - 1] != ",":
        return False
    # Only a short configuration list can extend a marker. Bound scanning
    # independently of the maximum model-response size and number of claims.
    if clause_end > 4096 or clause_end - clause_start > 512:
        return False
    pieces = sentence[:clause_start - 1].split(",", 16)
    if len(pieces) > 16:
        return False

    def item_only(text):
        spans = [found.span() for pattern in (_SIZE_RE, _FIT_RE, _COLOR_RE)
            for found in pattern.finditer(text) if _positive_configuration_value(text, found)]
        if not spans:
            return False
        remainder = list(text)
        for start, end in spans:
            remainder[start:end] = " " * (end - start)
        return bool(re.fullmatch(r"\s*(?:(?:and|the|a|an|та|і|и)\s*)*", "".join(remainder), re.I))

    if not item_only(sentence[clause_start:clause_end]):
        return False
    for index in range(len(pieces) - 1, -1, -1):
        head = pieces[index]
        choice = _CUSTOMER_CHOICE_RE.search(head)
        if choice:
            prefix = head[:choice.start()].strip()
            if prefix and not re.fullmatch(
                r"(?:i(?:['’]ve)?|we(?:['’]ve)?|я|ми|мы)(?:\s+have)?(?:\s+just)?|(?:as\s+)?you|as", prefix, re.I):
                return False
            if (_locally_negated(head, choice.start()) or re.search(
                r"\b(?:не|ні|not|no|never|(?:do|does|did|has|have|had|is|are|was|were|ca|wo|could|would|should|must|need)n['’]t)\b",
                _QUOTED_SPAN_RE.sub(" ", head), re.I) or re.search(
                r";|\b(?:але|но|but|and\s+we|і\s+ми|и\s+мы)\b", head, re.I)):
                return False
            return all(item_only(piece) for piece in pieces[index + 1:])
        if not item_only(head):
            return False
    return False


def _pending_manager_stock_check(clause: str):
    """Parse an own pending-check sentence; return references, never authority.

    Availability may be a fronted noun topic, separated from its future
    manager check by a comma. Only that noun belongs to the check predicate;
    an independent stock statement cannot inherit its nonassertive status.
    """
    from management.services.ig_reply_language import own_reply_text
    raw = str(clause or "")
    if not raw or len(raw) > 1000:
        return {}
    text = own_reply_text(raw).strip().rstrip(".!").strip()
    if (not text or re.search(r"[;?!\n]|\b(?:не|ні|not|never|if|would|could|якщо|якби|если|вчера|учора|раніше|раньше|previously|already)\b|n['’]t\b", text, re.I)
        or _MONEY_RE.search(text) or _TIMING_RE.search(text)
        or re.search(r"підтвердж\w*|подтвержд\w*|confirmed|гарант\w*|guarantee\w*|"
            r"доставк\w*|shipping|delivery|ц[іе]н\w*|price|payment|оплат\w*", text, re.I)):
        return {}
    noun = re.compile(r"\b(?:наявн(?:ість|ості)|налич(?:ие|ия|ию)|доступн(?:ість|ості|ость|ости)|availability)\b", re.I)
    nouns = list(noun.finditer(text))
    original_nouns = list(noun.finditer(raw))
    if len(nouns) != 1 or len(original_nouns) != 1:
        return {}
    action = re.compile(r"\b(?:(?:(?:зараз|наразі|сейчас|також|также)\s+)?(?:(?:я|ми)\s+)?"
        r"(?:(?:зараз|наразі|сейчас|також|также)\s+)?(?:уточню|уточнимо|уточним|перевірю|перевіримо|проверю|проверим|перевіряю|перевіряємо|проверяю|проверяем)|"
        r"(?:i|we)(?:['’]ll|\s+(?:will|shall|am|are))\s+(?:(?:now|also)\s+)?(?:check(?:ing)?|verify(?:ing)?))\b", re.I)
    # The role names an internal check recipient, never stock authority.
    # A team must be explicitly ours; a customer's/foreign team is not this
    # business's pending manager workflow.
    manager = re.compile(r"\b(?:у|з|із|с|со|with)\s+(?:"
        r"(?:(?:нашого|нашим|нашего|our|the)\s+)?(?:менеджер(?:а|ом|у)?|manager)|"
        r"(?:нашою\s+командою|нашей\s+командой|our\s+team))\b", re.I)
    actions, managers = list(action.finditer(text)), list(manager.finditer(text))
    if len(actions) != 1 or len(managers) != 1:
        return {}
    callback = re.search(r"\s+(?:та|й|і|и|and)\s+(?:"
        r"(?:(?:я|ми)\s+)?(?:(?:одразу|відразу|сразу)\s+)?(?:(?:вам|вас)\s+)?"
        r"(?:повідомлю|повідомимо|сообщу|сообщим)(?:\s+(?:вам|вас))?|"
        r"дам(?:о)?\s+вам\s+знати|"
        r"(?:(?:i|we)(?:['’]ll|\s+will)?\s+)?(?:let\s+you\s+know|notify\s+you))\s*$", text, re.I)
    spans = [actions[0].span(), managers[0].span()]
    if callback:
        spans.append(callback.span())
    subject = text
    for start, end in sorted(spans, reverse=True):
        subject = subject[:start] + " " * (end - start) + subject[end:]
    subject = re.sub(r"^\s*(?:що\s+стосується|щодо|стосовно|что\s+касается|по|насч[её]т|"
        r"regarding|concerning|as\s+for|about)\s+", "", subject, flags=re.I).strip(" ,—–-")
    # "Physical" qualifies the adjacent availability noun, not a new
    # assertion. Do not admit it as a free-standing residual adjective.
    subject = re.sub(r"\b(?:physical|фізичн(?:а|ої|у|ій)|"
        r"физическ(?:ая|ой|ую|ом|ое|ого|ому))\s+(?=" + noun.pattern + ")", "", subject, flags=re.I)
    # What remains must be one nonverbal availability topic, not a report,
    # conditional, second action, confirmed stock or an appended yes/no/wait.
    if (not noun.search(subject) or re.search(r"[,;:]|\b(?:я|ми|i|we|said|wrote|says|напис\w*|сказ\w*|"
            r"повідом\w*|сообщ\w*|є|есть|маємо|имеем|available|unavailable|sold|out|will|"
            r"немає|нема|нет|буде|будут|будет|очіку\w*|ожида\w*|під\s+замовлення|под\s+заказ)\b", subject, re.I)):
        return {}
    # Positively admit a noun-reference grammar, rather than trying to list
    # every adjective or verb that could assert stock. Unknown predicates
    # such as "стовідсоткова" / "забезпечена" remain protected claims.
    from management.services.ig_response_cart_plan import _scoped_request_tokens
    from management.services.ig_response_plan import CHOICE_ALIASES
    reference = re.sub(r"\bin\s+stock\b", '#firm-stock#', subject, flags=re.I)
    reference = noun.sub('availability', reference)
    reference = re.sub(r"\b(?:розмір(?:у|і|ом)?|размер(?:а|е|ом)?)\b", 'size', reference, flags=re.I)
    reference = re.sub(r"(?<!\w)(?:XS|S|M|L|XL|XXL|XXXL|[2-5]\s*[xх]\s*[lл])(?!\w)", ' ', reference, flags=re.I)
    reference = _FIT_RE.sub(lambda match: ' ' if _configuration_axis_value('fit',
        match.group('value') or match.group('descriptive_fit')) in {'classic', 'oversize'} else match.group(), reference)
    for key, aliases in CHOICE_ALIASES.items():
        if key in {'hoodie', 'tshirt', 'classic', 'oversize'}:
            continue
        for alias in aliases:
            reference = re.sub(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", ' ', reference, flags=re.I)
    reference = re.sub(r"\b(?:на\s+склад[іе]|at\s+(?:the\s+)?warehouse|саме|именно|exactly)\b", '', reference, flags=re.I)
    if not _scoped_request_tokens(reference, choices={}):
        return {}
    original_noun = original_nouns[0]
    return {"subject": subject, "availability_span": original_noun.span()}


def _source_owned_availability_check(clause: str) -> bool:
    """A check may repeat source choices without claiming they are sellable."""
    return bool(_pending_manager_stock_check(clause) or (_AVAILABILITY_CHECK_RE.search(clause)
        and _AVAILABILITY_CHECK_SUBJECT_RE.search(clause)
        and not _product_availability_claim(clause)))


def _descriptive_palette_reference(sentence: str, match: re.Match) -> bool:
    # A catalog aid is not a color named palette. Explicit/selected values
    # retain the existing configuration parser and source-ownership gates.
    return bool(match.re is _COLOR_RE and _normalize(match.group("value")) == "palette"
        and not match.group("selected") and not match.group("separator")
        and re.match(r"\s+is\s+(?:shown|displayed|listed)\b", sentence[match.end():], re.I))


def _is_qualified_standard_dispatch_timing(
    sentence: str,
    timing: str,
    context: ReplyTruthContext,
) -> bool:
    if context.fulfillment_review_status in {"pending", "reviewed"}:
        return False
    configured = context.explicitly_qualified_standard_dispatch_days
    if (
        not isinstance(configured, tuple)
        or len(configured) != 2
        or any(isinstance(value, bool) or not isinstance(value, int) for value in configured)
    ):
        return False
    duration = re.sub(r"^(?:within|in|до)\s+", "", str(timing or "").strip(), flags=re.I)
    match = _STANDARD_TIMING_RANGE_RE.fullmatch(duration)
    if match is None or (int(match.group("low")), int(match.group("high"))) != configured:
        return False
    if (
        not _USUAL_QUALIFIER_RE.search(sentence)
        or _timing_domain(sentence) != "preparation"
        or _TIMING_GUARANTEE_RE.search(sentence)
        or _CARRIER_DELIVERY_RE.search(sentence)
    ):
        return False
    return bool(context.payment_confirmed or _AFTER_PAYMENT_RE.search(sentence))


def _timing_domain(sentence: str) -> str:
    base_arrival = _BASE_ARRIVAL_RE.search(sentence)
    if base_arrival:
        # A base arriving at our workshop does not prove shipment or arrival
        # to the customer. A neighboring different promise retains its domain.
        other = _BASE_ARRIVAL_RE.sub("", sentence)
        if not any(pattern.search(other) for pattern in (
            _ARRIVAL_RE, _DEADLINE_FEASIBILITY_RE, _CARRIER_DELIVERY_RE,
            _PREPARATION_RE, _DISPATCH_RE,
        )):
            return "base_arrival"
    if _ARRIVAL_RE.search(sentence) or _DEADLINE_FEASIBILITY_RE.search(sentence):
        return "arrival_feasibility"
    if _CARRIER_DELIVERY_RE.search(sentence):
        return "carrier_estimate"
    if _PREPARATION_RE.search(sentence):
        return "preparation"
    if _DISPATCH_RE.search(sentence):
        return "dispatch"
    return ""


def _timing_scope(sentence: str, start: int) -> str:
    """Keep a preceding preparation duration separate from a later arrival."""
    clauses = list(re.finditer(r"[,;]", sentence[:start]))
    begin = clauses[-1].end() if clauses else 0
    tail = re.search(r"[,;]", sentence[start:])
    end = start + tail.start() if tail else len(sentence)
    local = sentence[begin:end]
    return local if _timing_domain(local) else sentence[:end]


def _timing_nonclaim(sentence: str, start: int) -> bool:
    return bool(
        _TIMING_HISTORY_RE.search(sentence[:start])
        or any(match.start() <= start for match in _TIMING_UNCERTAINTY_RE.finditer(sentence))
        or any(match.start() <= start for match in _TIMING_ENQUIRY_RE.finditer(sentence))
        or _locally_negated(sentence, start)
    )


def _timing_start_matches(sentence: str, authority: TimingAuthority) -> bool:
    """Explicit start dates must match the captured Kyiv day, never our clock."""
    starts = list(islice(_TIMING_START_RE.finditer(sentence), 9))
    if not starts:
        return True
    if len(starts) > 8:
        return False
    try:
        anchor = datetime.fromisoformat(str(authority.anchor_at).replace("Z", "+00:00"))
        if anchor.tzinfo is None or anchor.utcoffset() is None:
            return False
        anchored = anchor.astimezone(ZoneInfo("Europe/Kyiv")).date()
    except (ValueError, TypeError, OverflowError):
        # An existing exact source assertion can own its explicit date; a
        # bare approved duration cannot supply a newly appended start date.
        return bool(authority.claim and _normalize(authority.claim) == _normalize(sentence))
    for match in starts:
        value = match.group("date").casefold()
        year = None
        try:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                year, month, day = map(int, value.split("-"))
            elif re.fullmatch(r"\d{1,2}[.-]\d{1,2}(?:[.-]\d{2,4})?", value):
                parts = list(map(int, re.split(r"[.-]", value)))
                day, month = parts[:2]
                year = parts[2] if len(parts) == 3 else None
                if year is not None and year < 1000:
                    return False
            else:
                named = re.fullmatch(r"(\d{1,2})(?:(?:st|nd|rd|th)|-го)?\s+([^\W\d_]{3,16})(?:\s*,?\s*(\d{4}))?", value)
                if named:
                    day, name, raw_year = named.groups()
                else:
                    named = re.fullmatch(r"([^\W\d_]{3,16})\s+(\d{1,2})(?:st|nd|rd|th)?(?:\s*,?\s*(\d{4}))?", value)
                    if not named:
                        return False
                    name, day, raw_year = named.groups()
                month = _MONTHS.get(name)
                day, year = int(day), int(raw_year) if raw_year else None
                if month is None:
                    return False
            # An omitted year makes only a month/day claim. Never resolve a
            # relative day or incomplete bare day against the current date.
            claimed = date(year if year is not None else anchored.year, month, day)
            if claimed.month != anchored.month or claimed.day != anchored.day or year is not None and claimed.year != anchored.year:
                return False
        except (TypeError, ValueError, OverflowError):
            return False
    return True


def _typed_timing_allowed(
    sentence: str, timing: str, domain: str, context: ReplyTruthContext, *, anchor_only=False,
) -> bool:
    named_lines = {
        line_id for fact in context.timing_authorities if isinstance(fact, TimingAuthority)
        for line_id, label in fact.review_line_labels
        if label and _normalize(label) in _normalize(sentence)
    }
    for authority in context.timing_authorities:
        if not isinstance(authority, TimingAuthority) or authority.domain != domain:
            continue
        if domain == "base_arrival":
            if not authority.source_ref or not authority.line_id or authority.anchor:
                continue
            try:
                anchor = datetime.fromisoformat(authority.anchor_at.replace("Z", "+00:00"))
                if anchor.tzinfo is None or anchor.utcoffset() is None:
                    continue
            except (ValueError, TypeError, AttributeError):
                continue
        if authority.line_id and (
            not authority.line_label
            or _normalize(authority.line_label) not in _normalize(sentence)
        ):
            continue
        if authority.line_id and named_lines - {authority.line_id}:
            # A combined assertion must be independently true for every named
            # item; one reviewed line cannot confer authority on the others.
            if any(not any(
                isinstance(other, TimingAuthority) and other.line_id == line_id
                and other.domain == domain
                and (other.min_days, other.max_days, other.unit, other.anchor, other.anchor_at)
                    == (authority.min_days, authority.max_days, authority.unit, authority.anchor, authority.anchor_at)
                for other in context.timing_authorities
            ) for line_id in named_lines):
                continue
        if not _timing_start_matches(sentence, authority):
            continue
        if (authority.anchor == "after_payment_confirmation" and not context.payment_confirmed
                and not _AFTER_PAYMENT_RE.search(sentence)):
            # A conditional preparation estimate cannot silently start now.
            # Payment settlement remains independently verified by the context.
            continue
        if authority.claim:
            # Feasibility requires the actual source-backed assertion. A bare
            # duration can never authorize "we will make it" or an arrival.
            if _normalize(authority.claim) == _normalize(sentence):
                return domain != "carrier_estimate" or bool(
                    _CARRIER_ESTIMATE_QUALIFIER_RE.search(sentence)
                    and not _TIMING_GUARANTEE_RE.search(sentence)
                )
            if domain != "arrival_feasibility" and _normalize(authority.claim) == _normalize(timing):
                if domain != "carrier_estimate" or _CARRIER_ESTIMATE_QUALIFIER_RE.search(sentence):
                    return not bool(_TIMING_GUARANTEE_RE.search(sentence))
        if domain == "arrival_feasibility" or _TIMING_GUARANTEE_RE.search(sentence):
            continue
        if anchor_only:
            if authority.anchor_at and (domain != "carrier_estimate" or _CARRIER_ESTIMATE_QUALIFIER_RE.search(sentence)):
                return True
            continue
        duration = re.sub(r"^(?:within|in|до)\s+", "", timing.strip(), flags=re.I)
        match = _STANDARD_TIMING_RANGE_RE.fullmatch(duration)
        if match is not None:
            low, high = int(match.group("low")), int(match.group("high"))
        else:
            single = re.fullmatch(
                r"(?:до\s+|within\s+|in\s+)?(\d{1,3})\s*"
                r"(?:(?:робоч\w*|рабоч\w*|business)\s+)?"
                r"(?:дн(?:і|ів|я)|день|дня|дней|days?)", timing.strip(), re.I,
            )
            if single is None:
                continue
            low = high = int(single.group(1))
        if (low, high) != (authority.min_days, authority.max_days):
            continue
        business_days = bool(re.search(r"робоч\w*|рабоч\w*|business", timing, re.I))
        if business_days != (authority.unit == "business_days"):
            continue
        if authority.unit not in {"calendar_days", "business_days", "days"}:
            continue
        if domain == "carrier_estimate" and not _CARRIER_ESTIMATE_QUALIFIER_RE.search(sentence):
            continue
        if _AFTER_PAYMENT_RE.search(sentence) and authority.anchor not in {"payment", "after_payment", "payment_confirmation", "after_payment_confirmation"}:
            continue
        return True
    return False


def _legacy_timing_allowed(sentence: str, timing: str, context: ReplyTruthContext) -> bool:
    if context.fulfillment_review_status in {"pending", "reviewed"}:
        return False
    # Compatibility for established server timing inputs, with no promotion to
    # arrival feasibility or bare deadline guarantees.
    if _timing_domain(sentence) in {"arrival_feasibility", "base_arrival"}:
        return False
    return _normalize(timing) in {_normalize(value) for value in context.approved_timing_claims}


def _subject_token(sentence: str, value: str) -> bool:
    return bool(value and re.search(r"(?<!\w)" + re.escape(_normalize(value)) + r"(?!\w)", _normalize(sentence)))


def _availability_claim_statuses(sentence: str) -> tuple[str, ...]:
    """Physical availability, absence and waiting are equally protected facts."""
    # Remove only the photo-existence predicate, preserving positions and all
    # neighboring clauses. A greedy stock regex must not let "є фото oversize"
    # hide a later independent "є розмір M" assertion in the same sentence.
    stock_sentence = _PHOTO_EXISTENCE_RE.sub(lambda match: " " * len(match.group()), sentence)
    stock_sentence = _PHOTO_ABSENCE_RE.sub(lambda match: " " * len(match.group()), stock_sentence)
    stock_sentence = _CATALOG_EXISTENCE_RE.sub(lambda match: " " * len(match.group()), stock_sentence)
    quoted_spans = tuple(_QUOTED_SPAN_RE.finditer(stock_sentence))
    pending_check = _pending_manager_stock_check(stock_sentence)
    statuses = []
    subject = bool(_STOCK_SUBJECT_RE.search(stock_sentence) or _EXACT_STOCK_RE.search(stock_sentence)
        or re.search(r"\bout\s+of\s+stock\b|\bsold\s+out\b", stock_sentence, re.I))
    for status, pattern in (("unavailable", _NEGATIVE_STOCK_RE), ("wait", _WAIT_STOCK_RE)):
        for match in pattern.finditer(stock_sentence):
            prefix = re.split(r"[,;]|\b(?:але|но|but|however)\b", stock_sentence[:match.start()], flags=re.I)[-1]
            polite = status == "unavailable" and re.match(
                r"\s+(?:проблем\w*|за\s+що|за\s+что)\b", stock_sentence[match.end():], re.I,
            )
            # A product title such as «Правил немає» names the item. Its
            # quoted lexical negation is not a statement of physical stock.
            title_negation = status == "unavailable" and match.group().casefold() in {"немає", "нема", "нет"} and any(
                quote.start() <= match.start() < quote.end() for quote in quoted_spans)
            nonphysical = status == "unavailable" and _NONPHYSICAL_ABSENCE_RE.match(stock_sentence[match.end():])
            if subject and not (polite or title_negation or nonphysical) and not _STOCK_NONASSERTION_RE.search(prefix):
                if status not in statuses:
                    statuses.append(status)
    for match in _NOMINAL_STOCK_RE.finditer(stock_sentence):
        prefix = re.split(r"[,;]|\b(?:але|но|but|however)\b", stock_sentence[:match.start()], flags=re.I)[-1]
        assertion_start = match.start('after') if match.group('after') else match.start('before')
        # "not certain" is a qualification, not a positive stock assurance.
        # Existing independent stock/absence claims are still inspected below.
        if (_STOCK_SUBJECT_RE.search(match.group())
            and not _locally_negated(stock_sentence, assertion_start)
            and not _locally_negated(stock_sentence, match.start())
            and not _STOCK_NONASSERTION_RE.search(prefix)
            and not _NEGATED_STATUS_RE.search(match.group())):
            if "available" not in statuses:
                statuses.append("available")
    for pattern in (_AVAILABILITY_RE, _EXACT_STOCK_RE, _BARE_PRODUCT_STOCK_RE):
        for match in pattern.finditer(stock_sentence):
            prefix = re.split(r"[,;]|\b(?:але|но|but|however)\b", stock_sentence[:match.start()], flags=re.I)[-1]
            tail = re.split(r"[,;]", stock_sentence[match.end():], maxsplit=1)[0]
            pending_subject = (re.match(r"(?:наявність|наявності|доступність|наличие|наличия|availability)\b", match.group(), re.I)
                and _AVAILABILITY_CHECK_RE.search(tail)
                and not re.search(r"\b(?:є|есть|available)\b|in\s+stock", match.group(), re.I))
            if pending_check:
                noun_start, noun_end = pending_check["availability_span"]
                pending_subject = pending_subject or match.start() <= noun_start < noun_end <= match.end()
            if (
                not _locally_negated(stock_sentence, match.start())
                and not _NEGATED_STATUS_RE.search(match.group(0))
                and not _STOCK_NONASSERTION_RE.search(prefix)
                and not _NEGATIVE_STOCK_RE.search(match.group(0))
                and not pending_subject
            ):
                if "available" not in statuses:
                    statuses.append("available")
    return tuple(statuses)


def _product_availability_claim(sentence: str) -> bool:
    return any(_availability_claim_statuses(clause) for clause in _STOCK_CLAUSE_BOUNDARY_RE.split(sentence))


def _availability_line_reference_matches(sentence: str, fact: AvailabilityAuthority) -> bool:
    return bool(fact.line_id and re.search(
        r"(?:позиція|позиция|поз\.?|line|item)\s*#?\s*" + re.escape(fact.line_id) + r"(?!\w)", sentence, re.I,
    ))


def _availability_scope_matches(sentence: str, fact: AvailabilityAuthority) -> bool:
    if fact.line_label and _subject_token(sentence, fact.line_label):
        return True
    if _availability_line_reference_matches(sentence, fact):
        return True
    from management.services.ig_conversation_agreement import _configuration
    given, _ = _configuration(sentence)
    color, _ = _configuration(fact.color_label)
    color_matches = _subject_token(sentence, fact.color_label) or bool(
        color.get("color") and given.get("color") == color["color"])
    return bool(
        fact.product_label and fact.color_label
        and _subject_token(sentence, fact.product_label)
        and color_matches
        and (_subject_token(sentence, fact.size)
            or given.get("size") is not None and _configuration_axis_value("size", given["size"])
                == _configuration_axis_value("size", fact.size))
    )


def _availability_configuration_matches(
    sentence: str, fact: AvailabilityAuthority, *, expected_availability="available",
) -> bool:
    # Check explicit labels and familiar free-form values, without allowing a
    # matching product name to hide a different size, fit, color or quantity.
    from management.services.ig_conversation_agreement import _configuration
    if expected_availability == "unavailable":
        # "L is not available" negates stock, not the customer's size choice.
        # Preserve unrelated negations such as "not black" for the parser.
        sentence = _NEGATIVE_STOCK_RE.sub(lambda match: " " * len(match.group()), sentence)
    stated, ambiguous = _configuration(sentence)
    if ambiguous:
        return False
    for axis, expected in (("size", fact.size), ("fit", fact.fit_code), ("color", fact.color_label)):
        if axis not in stated:
            continue
        normalized, _ = _configuration(expected)
        if stated[axis] != normalized.get(axis, _normalize(expected)):
            return False
    for axis, pattern, expected in (("size", _SIZE_RE, fact.size), ("fit", _FIT_RE, fact.fit_code), ("color", _COLOR_RE, fact.color_label)):
        for match in pattern.finditer(sentence):
            value = _positive_configuration_value(sentence, match)
            if not value or _locally_negated(sentence, match.start()):
                continue
            given, _ = _configuration(value)
            wanted, _ = _configuration(expected)
            if given.get(axis, _normalize(value)) != wanted.get(axis, _normalize(expected)):
                return False
    return all(int(first or second) == fact.quantity for first, second in _STOCK_QUANTITY_RE.findall(sentence))


_COMPLETED_STOCK_CHECK_RE = re.compile(
    r"(?:менедж\w*|команд\w*|склад\w*)[^.!?\n]{0,35}(?:перевір(?:ив|ила|или|ено)|провер(?:ил|ила|или|ено)|підтверд(?:ив|ила|или|жено)|подтверд(?:ил|ила|или|ено))\b|"
    r"(?:manager|team|warehouse)[^.!?\n]{0,35}(?:checked|verified|confirmed)|"
    r"(?:я|ми|I|we)[^.!?\n]{0,20}(?:особисто|лично|фізично|физически|physically)[^.!?\n]{0,20}(?:перевір\w*|провер\w*|checked|verified)|"
    r"(?:I|we)\s+(?:have\s+)?(?:checked|verified)\s+(?:the\s+)?(?:warehouse|physical\s+stock)", re.I)


def _manager_availability_matches(
    sentence: str, context: ReplyTruthContext, *, expected_availability: str | None = None,
) -> tuple[AvailabilityAuthority, ...]:
    clauses = [clause for clause in _STOCK_CLAUSE_BOUNDARY_RE.split(sentence)
        if _availability_claim_statuses(clause)]
    if len(clauses) > 1:
        matches = [_manager_availability_matches(clause, context, expected_availability=expected_availability) for clause in clauses]
        return tuple(dict.fromkeys(fact for group in matches for fact in group)) if all(matches) else ()
    statuses = _availability_claim_statuses(clauses[0] if clauses else sentence)
    if len(statuses) > 1 or expected_availability and statuses and statuses != (expected_availability,):
        return ()
    # Configuration-only clauses ("color black") inherit their protected
    # sentence's fact; they do not themselves assert physical availability.
    expected_availability = expected_availability or (statuses[0] if statuses else "available")
    manager_facts = tuple(fact for fact in context.availability_authorities
        if type(fact) is AvailabilityAuthority and context.fulfillment_review_status == "reviewed")
    inventory_facts = tuple(fact for fact in context.inventory_availability_authorities
        if type(fact) is InventoryAvailabilityAuthority)
    # A current manager decision remains authoritative for its own line.
    managed = {fact.line_id for fact in manager_facts}
    facts = (*manager_facts, *(fact for fact in inventory_facts if fact.line_id not in managed))
    if not facts:
        return ()
    if (_WHOLE_CART_STOCK_RE.search(sentence) and context.inventory_cart_line_ids
        and not set(context.inventory_cart_line_ids).issubset({fact.line_id for fact in facts})):
        return ()
    named = facts if _WHOLE_CART_STOCK_RE.search(sentence) else tuple(
        fact for fact in facts if _availability_scope_matches(sentence, fact))
    if not named:
        if len(facts) == 1 and not re.search(r"футболк\w*|худі|худи|t[- ]?shirt\w*|hoodie\w*|product\w*|товар\w*", sentence, re.I):
            named = facts
        else:
            return ()
    for fact in named:
        inventory = type(fact) is InventoryAvailabilityAuthority
        if (
            fact.availability != expected_availability or not fact.source_ref
            or type(fact.quantity) is not int or not 1 <= fact.quantity <= 99
            or inventory and (expected_availability != "available"
                or _COMPLETED_STOCK_CHECK_RE.search(sentence)
                or fact.supply_basis != "inventory_observation"
                or not all(re.fullmatch(r"[a-f0-9]{64}", str(value)) for value in (
                    fact.inventory_digest, fact.configuration_digest,
                    fact.source_cart_capture_digest, fact.capture_digest)))
            or expected_availability == "available" and (
                fact.supply_basis not in ({"inventory_observation"} if inventory else {"warehouse_checked", "accessible_unrecorded"})
                or type(fact.checked_quantity) is not int
                or not fact.quantity <= fact.checked_quantity <= (1000000 if inventory else 999)
            )
            or not _availability_configuration_matches(sentence, fact, expected_availability=expected_availability)
        ):
            return ()
    # Even a partial exact label is ambiguous when another requested line has
    # the same product/color/size but a different fit or option combination.
    if not _WHOLE_CART_STOCK_RE.search(sentence):
        for fact in named:
            for other in facts:
                if other in named:
                    continue
                if (other.product_id, other.color_variant_id, other.size) == (fact.product_id, fact.color_variant_id, fact.size):
                    if not _subject_token(sentence, fact.line_label) and not _availability_line_reference_matches(sentence, fact) and not (
                        fact.fit_label and _subject_token(sentence, fact.fit_label)
                        and fact.fit_code != other.fit_code
                    ):
                        return ()
    return named


_PRODUCT_FIBERS = {
    "cotton": re.compile(r"бавовн\w*|хлоп(?:ок|к\w*)|cotton", re.I),
    "elastane": re.compile(r"еластан\w*|эластан\w*|elastane|spandex", re.I),
    "polyester": re.compile(r"поліестер\w*|полиэстер\w*|polyester", re.I),
    "wool": re.compile(r"\bвовн\w*|\bшерст\w*|\bwool\b", re.I),
    "viscose": re.compile(r"в[іи]скоз\w*|\bviscose\b|\brayon\b", re.I),
    "linen": re.compile(r"\bльон\w*|\bл[еь]н\b|\blinen\b", re.I),
    "acrylic": re.compile(r"акрил\w*|\bacrylic\b", re.I),
    "nylon": re.compile(r"нейлон\w*|\bnylon\b", re.I),
    "silk": re.compile(r"шовк\w*|ш[её]лк\w*|\bsilk\b", re.I),
    "synthetic": re.compile(r"\bсинтет\w*|\bsynthetic\w*", re.I),
}
_PRODUCT_FABRIC = re.compile(r"кул[іи]р\w*|пень[єе]\w*|тр[иіёе]х?нит\w*|фл[іи]с\w*|fleece|kulir|combed|three[- ]thread", re.I)
_PRODUCT_DENSITY = re.compile(r"(?P<gsm>\d{2,4})\s*(?:г\s*/\s*м[²2]?|g\s*/\s*m[²2]?|gsm)(?!\w)", re.I)
_PRODUCT_TEMPERATURE = re.compile(
    r"(?<!\w)(?P<degrees>\d{2,3})\s*(?:°\s*[cс]|градус\w*|degrees?(?:\s+celsius)?)(?!\w)", re.I)
_PRODUCT_WASHES = re.compile(r"(?P<count>\d+)\s*\+?\s*(?:пран\w*|стир\w*|wash\w*)", re.I)
_PRODUCT_OWNER = re.compile(r"засновник\w*|власник\w*|основател\w*|владел\w*|founder|owner", re.I)
_PRODUCT_CARE = re.compile(r"догляд\w*|уход\w*|рекоменд\w*|recommended\s+care|care\s+guidance", re.I)
_PRODUCT_UNSUPPORTED_QUALITY = re.compile(r"лаборатор\w*|laboratory|lab[- ]tested|найкращ\w*\s+в\s+україн\w*|лучш\w*\s+в\s+украин\w*|best\s+in\s+ukraine|вічн\w*|вечн\w*|eternal|ніколи\s+не\s+(?:тріск\w*|вицвіт\w*|вигор\w*|линя\w*)|никогда\s+не\s+(?:треск\w*|выцвет\w*|выгор\w*|линя\w*)|never\s+(?:cracks?|fades?|discolou?rs?)|(?:не\s+(?:вицвіт\w*|вигор\w*|линя\w*)\s+ніколи|не\s+(?:выцвет\w*|выгор\w*|линя\w*)\s+никогда)|не\s+сіда\w*\s+(?:взагалі|зовсім)|вообще\s+не\s+сад\w*|never\s+shrinks?", re.I)
_PRODUCT_UNCERTAIN = re.compile(r"уточн\w*|перевір\w*|провер\w*|не\s+(?:зна\w*|підтвердж\w*|подтвержд\w*)|cannot\s+confirm|don't\s+know|do\s+not\s+know|will\s+check|i['’]ll\s+check|let\s+me\s+check", re.I)
_DESIGN_STORY_SUBJECT = re.compile(r"\b(?:принт\w*|дизайн\w*|малюн\w*|рисунк\w*|ілюстрац\w*|иллюстрац\w*|prints?|designs?|artwork)\b", re.I)
_DESIGN_STORY_CLAIM = re.compile(
    r"створ(?:или|ив|ила|ено|ений|ена|ене|ені)\b|созда(?:ли|л|ла|но|н\w*)\b|"
    r"намалюва(?:ли|в|ла)\b|намальован\w*|нарисова(?:ли|л|ла|н\w*)\b|присвяч\w*|посвящ\w*|натхнен\w*|вдохнов\w*|"
    r"символ[іи]з\w*|означає\w*|означает\w*|пов['’]?язан\w*|связан\w*|позивн\w*|позывн\w*|"
    r"\b(?:created|drawn|inspired|commemorates?|dedicated|symboli[sz]es?|represents?|means?|originated|callsign)\b|"
    r"(?:історі\w*|истори\w*|story|origin)[^.!?]{0,50}(?:почал\w*|начал\w*|поход\w*|произош\w*|began|comes?\s+from)", re.I)


def _approved_brand_originality(clause):
    """Only the published general brand claim; no SKU/story identity grant."""
    from management.services.approved_public_facts import approved_public_facts

    normalized = _normalize(clause).rstrip(".!;")
    for language in ("uk", "ru", "en"):
        fact = next((row for row in approved_public_facts(language) if row.key == "brand_originality"), None)
        if (fact is None or fact.scope != "public_brand_originality"
            or fact.source_ref != "owner:2026-10-18:Q08"
            or fact.version != "2026-10-18.owner-brand-originality.1"
            or fact.review_status != "reviewed:owner-brief:2026-10-18"):
            continue
        if normalized in {_normalize(sentence).rstrip(".!;") for sentence in _claim_sentences(fact.public_text, ())}:
            return True
        # Finite equivalent wording for the same plural brand claim. Full
        # matching excludes exact-print references, locations, causes, future
        # collections, custom artwork, competitors and appended claims.
        equivalent = {
            "uk": r"наші (?:принти|дизайни) (?:створені|намальовані|розроблені) з нуля",
            "ru": r"наши (?:принты|дизайны) (?:созданы|нарисованы|разработаны) с нуля",
            "en": r"our (?:prints|designs) are (?:created|drawn|developed) from scratch",
        }[language]
        if re.fullmatch(equivalent, normalized, re.I):
            return True
    return False


def _unverified_design_story(clause):
    """A selected garment is not an exact reviewed design/artwork story."""
    subjects = list(_DESIGN_STORY_SUBJECT.finditer(clause))
    if not subjects:
        return False
    claims = list(_DESIGN_STORY_CLAIM.finditer(clause))
    if not claims:
        return False
    # Generic hypothetical examples do not assert an origin for this SKU.
    if (re.search(r"наприклад|например|for\s+example", clause, re.I)
        and re.search(r"може|может|could|might|can\s+be", clause, re.I)
        and not re.search(r"\b(?:цей|ця|цього|цієї|этот|эта|этого|этой|this|your)\b", clause, re.I)):
        return False
    for match in claims:
        if _product_claim_negated(clause, match.start()):
            continue
        if re.fullmatch(r"означає\w*|означает\w*|means?", match.group(), re.I):
            preceding = [subject for subject in subjects if subject.end() <= match.start()]
            if not preceding or re.search(r"\b(?:regular|classic|oversize|крій|крою|крой|розмір\w*|размер\w*|fit|cut|size)\b",
                clause[preceding[-1].end():match.start()], re.I):
                continue
        return True
    return False
_PRODUCT_PERCENT_UNIT = r"(?:%|відсот\w*|процент\w*|percent(?:age)?|per\s+cent)"
_PRODUCT_PURITY = re.compile(
    r"\b(?:повністю|цілком|виключно|полностью|целиком|исключительно|стовідсот\w*|"
    r"стопроцент\w*|entirely|wholly)\b|"
    r"\b(?:сто\s+(?:відсот\w*|процент\w*)|one\s+hundred\s+(?:percent|per\s+cent))\b|"
    r"\b(?:без\s+(?:доміш\w*|примес\w*|синтетик\w*)|without\s+(?:any\s+)?(?:additives|admixtures|synthetics))\b",
    re.I)


def _product_fiber_amount(clause, fiber_match):
    """Interpret customer-facing percentages and purity against one fiber."""
    before, after = clause[:fiber_match.start()], clause[fiber_match.end():]
    amount = re.search(rf"(?<!\w)(\d{{1,3}}(?:[.,]\d+)?)\s*{_PRODUCT_PERCENT_UNIT}\s*(?:\w+\s+)?$", before, re.I)
    if amount is None:
        amount = re.match(rf"\W*(?:(?:становить|складає|составляет|is)\s+)?(\d{{1,3}}(?:[.,]\d+)?)\s*{_PRODUCT_PERCENT_UNIT}", after, re.I)
    if amount is not None:
        return _decimal(amount.group(1))
    exclusive = re.search(r"\b(?:all|only|pure|чист(?:а|ий|ої|ого|ая|ый|ой))(?:[-\s]+(?:natural|organic|натуральн\w*))?[-\s]+$", before, re.I)
    if exclusive is not None and not _product_claim_negated(clause, exclusive.start()):
        return Decimal("100")
    if re.match(r"\s+(?:only|is\s+pure)\b", after, re.I):
        return Decimal("100")
    # A purity promise is an effective 100% claim. Keep its own negation,
    # rather than allowing 'not pure cotton' to acquire a positive promise.
    nearby = clause[max(0, fiber_match.start() - 50):fiber_match.end() + 50]
    offset = max(0, fiber_match.start() - 50)
    if any(not _product_claim_negated(clause, offset + match.start())
           for match in _PRODUCT_PURITY.finditer(nearby)):
        return Decimal("100")
    return None


def _product_claim_negated(clause, start):
    before = clause[max(0, start - 70):start]
    return (_locally_negated(clause, start) or bool(re.search(
        rf"\b(?:не|not|no|without)\s+(?:(?:з|із|из|from|made|sewn|пошит\w*|сшит\w*|виготовлен\w*)\s+)?(?:\d{{1,3}}(?:[.,]\d+)?\s*{_PRODUCT_PERCENT_UNIT}\s*)?$",
        before, re.I)))


def _product_statement_assertions(sentence, assertions, *, inherit_category=False):
    from management.services.approved_public_facts import _GARMENT_WORDS, _UNSCOPED

    if _UNSCOPED.search(sentence):
        return ()
    garments = {key for key, pattern in _GARMENT_WORDS.items() if pattern.search(sentence)}
    result = []
    for row in assertions:
        # A valid public-policy capability can answer its own policy topic
        # without authorizing garment specifications. Only these reviewed
        # base/category schemas carry material, density and quality authority.
        if row.get("schema") not in {"approved-base-assertion.v1", "approved-category-assertion.v1"}:
            continue
        if garments and garments != {row.get("garment")}:
            continue
        if row.get("scope_kind") == "current_category":
            qualified = bool(re.search(r"(?:наш[іи]\s+)?(?:поточн\w*|актуальн\w*|текущ\w*)|(?:our\s+)?current", sentence, re.I))
            specific = bool(re.search(r"\b(?:цей|ця|цю|цього|цієї|этот|эта|эту|этого|этой|this|that)\b", sentence, re.I))
            if specific or (not inherit_category and (not qualified or not garments)):
                continue
        result.append(row)
    return tuple(result)


def validate_approved_product_claims(reply_text, assertions=(), *, quoted_data=()):
    """Validate finite protected material/quality claims against one capture.

    This is deliberately a bounded claim inventory, not proof of arbitrary
    marketing prose. The same assertion/version also drives reply coverage.
    """
    from management.services.approved_public_facts import approved_assertion_valid

    safe = tuple(row for row in assertions if approved_assertion_valid(row))
    for sentence in _claim_sentences(str(reply_text or ""), quoted_data):
        if "?" in sentence:
            continue
        # A check commitment is not a material assertion. An independent
        # confident clause after it still needs proof.
        sentence_allowed = _product_statement_assertions(sentence, safe)
        for clause in re.split(r"[;:]|\b(?:але|но|but)\b", sentence, flags=re.I):
            if (_PRODUCT_UNCERTAIN.search(clause)
                and not re.search(r"(?:\b\d+\s*%|\b(?:зроблен\w*|сделан\w*|made|is|are|з|из)\b)", clause, re.I)
                and not any(pattern.search(clause) for pattern in _PRODUCT_FIBERS.values())):
                continue
            # Current editorial story leads have no reviewed design/version
            # identity. Neither a SKU/base proof nor a similar photo supplies
            # it. Add an exact publication capability before granting stories.
            if _unverified_design_story(clause) and not _approved_brand_originality(clause):
                return "unverified_product_story"
            allowed = _product_statement_assertions(clause, sentence_allowed, inherit_category=True)
            if any(row.get("scope_kind") == "current_category" and row.get("garment") == "hoodie"
                   and "current_hoodie_audience" in row.get("fact_keys", ()) for row in allowed):
                for match in re.finditer(r"\b(?:regular|classic|класич\w*|классичес\w*)\b", clause, re.I):
                    if not _product_claim_negated(clause, match.start()):
                        return "configuration_mismatch"
            for fiber, pattern in _PRODUCT_FIBERS.items():
                for match in pattern.finditer(clause):
                    if _product_claim_negated(clause, match.start()):
                        continue
                    amount = _product_fiber_amount(clause, match)
                    if not any(any(pair[1] == fiber and (amount is None or pair[0] == amount)
                                   for pair in row.get("composition", ())) for row in allowed):
                        return "unverified_product_material"
            for match in _PRODUCT_DENSITY.finditer(clause):
                gsm = int(match.group("gsm"))
                permitted = []
                for row in allowed:
                    if row.get("scope_kind") == "current_category" and row.get("garment") == "tshirt":
                        fits = list(re.finditer(r"regular|classic|oversize|класич\w*|классичес\w*|оверсайз\w*", clause[:match.start()], re.I))
                        expected = 220 if fits and re.search(r"oversize|оверсайз", fits[-1].group(), re.I) else 190 if fits else None
                        permitted.append(gsm == expected)
                    else:
                        permitted.append(row.get("gsm") == gsm)
                if not _product_claim_negated(clause, match.start()) and (not any(permitted)
                    or re.search(r"точн\w*|ровно|exact(?:ly)?|precisely", clause, re.I)):
                    return "unverified_product_material"
            for match in _PRODUCT_FABRIC.finditer(clause):
                if _product_claim_negated(clause, match.start()):
                    continue
                token = match.group().casefold()
                hoodie_only = bool(re.search(r"фл[іи]с|fleece|тр[иіёе]х?нит|three", token))
                tee_only = bool(re.search(r"кул[іи]р|kulir", token))
                if not allowed or (hoodie_only and not any(row["garment"] == "hoodie" for row in allowed)) or (tee_only and not any(row["garment"] == "tshirt" for row in allowed)):
                    return "unverified_product_material"
            origin = re.search(r"турецьк\w*|турецк\w*|туреч\w*|турци\w*|turkish|turkey", clause, re.I)
            manufacturing = re.search(r"(?:пошит\w*|зшит\w*|сшит\w*|виготовлен\w*|изготовлен\w*)\s+(?:в|у|во)\s+(?:україн\w*|украин\w*|кита\w*|туреч\w*|турци\w*)|(?:made|sewn)\s+in\s+(?:ukraine|china|turkey)", clause, re.I)
            manufacturing_negated = bool(manufacturing and _product_claim_negated(clause, manufacturing.start()))
            if origin and not _product_claim_negated(clause, origin.start()) and not manufacturing_negated:
                if (not any(row["garment"] == "tshirt" for row in allowed)
                    or re.search(r"пошит\w*|пошив\w*|зшит\w*|сшит\w*|sewn|made\s+in", clause, re.I)):
                    return "unverified_product_material"
            if manufacturing and not manufacturing_negated:
                return "unverified_product_material"
            for match in _PRODUCT_TEMPERATURE.finditer(clause):
                if not _locally_negated(clause, match.start()) and (int(match.group("degrees")) != 30
                    or not any(row.get("available_fact_versions", {}).get("current_care") for row in allowed)):
                    return "unverified_product_quality"
            for match in _PRODUCT_WASHES.finditer(clause):
                if _locally_negated(clause, match.start()):
                    continue
                if (int(match.group("count")) != 100
                    or not any(row.get("available_fact_versions", {}).get("current_print_quality") for row in allowed)
                    or not _PRODUCT_OWNER.search(clause)
                    or not _PRODUCT_CARE.search(clause) or not re.search(r"понад|більш\w*|более|over|more\s+than", clause, re.I)):
                    return "unverified_product_quality"
            for match in _PRODUCT_UNSUPPORTED_QUALITY.finditer(clause):
                if (not _locally_negated(clause, match.start())
                    and not re.search(r"не\s+(?:є|явля\w*|обіця\w*|обещ\w*|гаранту\w*|гарантир\w*)|not\s+(?:a\s+)?|(?:don't|do\s+not)\s+(?:promise|guarantee)", clause[:match.start()], re.I)):
                    return "unverified_product_quality"
    return ""


def approved_product_assertion_covered(reply_text, assertion, *, topic=None):
    """A correct answer must contain the requested facts, not just its topic."""
    from management.services.approved_public_facts import approved_assertion_valid

    if not approved_assertion_valid(assertion) or validate_approved_product_claims(reply_text, (assertion,)):
        return False
    clauses = [clause for clause in _claim_sentences(str(reply_text or ""), ())
               if "?" not in clause and not _PRODUCT_UNCERTAIN.search(clause)
               and _product_statement_assertions(clause, (assertion,))]
    text = " ".join(clauses)
    topic = topic or assertion["topic"]
    if topic in {"audience", "fit"}:
        if assertion.get("scope_kind") != "current_category":
            return False
        key = "current_hoodie_audience" if assertion.get("garment") == "hoodie" else "current_tshirt_audience"
        if key not in assertion.get("fact_keys", ()):
            return False
        if topic == "audience":
            return bool(re.search(r"ун[іи]секс|\bunisex\b", text, re.I))
        if assertion.get("garment") == "hoodie":
            return bool(re.search(r"(?:злегка|слегка|slightly)\s+(?:oversiz\w*|оверсайз\w*)", text, re.I)
                        and not re.search(r"regular|classic|класич\w*|классичес\w*", text, re.I))
        return bool(re.search(r"regular|classic|класич\w*|классичес\w*", text, re.I)
                    and re.search(r"oversize|оверсайз\w*", text, re.I))
    if topic == "composition":
        return all(any(_product_fiber_amount(clause, match) == amount
                       for clause in clauses for match in pattern.finditer(clause))
                   for amount, fiber in assertion["composition"] for pattern in (_PRODUCT_FIBERS[fiber],))
    if topic == "density":
        return any(int(match.group("gsm")) in assertion.get("allowed_gsm", [assertion["gsm"]]) for match in _PRODUCT_DENSITY.finditer(text))
    if topic == "material":
        return bool(_PRODUCT_FABRIC.search(text))
    if topic == "care":
        return bool(_PRODUCT_TEMPERATURE.search(text) and _PRODUCT_CARE.search(text))
    if topic == "quality":
        return bool(_PRODUCT_WASHES.search(text) and _PRODUCT_OWNER.search(text) and _PRODUCT_CARE.search(text))
    return False


def _approved_payment_policy_sentences(context):
    """Re-resolve a bounded reviewed proof; public prose grants no price right."""
    import hashlib
    from management.services.approved_public_facts import approved_public_facts

    proofs = context.approved_payment_policy
    if not isinstance(proofs, (tuple, list)) or len(proofs) > 3:
        return frozenset()
    sentences = set()
    for proof in proofs:
        if (not isinstance(proof, (tuple, list)) or len(proof) != 3
            or not all(isinstance(value, str) for value in proof)):
            continue
        language, version, digest = proof
        if language not in {"uk", "ru", "en"}:
            continue
        fact = next((row for row in approved_public_facts(language) if row.key == "payment_boundary"), None)
        if (fact is None or not fact.review_status.startswith("reviewed:") or fact.version != version
            or hashlib.sha256(fact.public_text.encode()).hexdigest() != digest):
            continue
        sentences.update(_normalize(sentence) for sentence in _claim_sentences(fact.public_text, ()))
    return frozenset(sentences)


def _approved_fulfillment_policy_sentences(context):
    """Recognize exact captured public policy, never a rewritten client claim."""
    import hashlib
    from management.services.approved_public_facts import approved_public_facts

    proofs = context.approved_fulfillment_policy
    if not isinstance(proofs, (tuple, list)) or len(proofs) > 3:
        return frozenset()
    sentences = set()
    for proof in proofs:
        if (not isinstance(proof, (tuple, list)) or len(proof) != 3
                or not all(isinstance(value, str) for value in proof)):
            continue
        language, version, digest = proof
        if language not in {"uk", "ru", "en"}:
            continue
        fact = next((row for row in approved_public_facts(language) if row.key == "dispatch"), None)
        if (fact is None or fact.scope != "current_tshirts_and_hoodies_preparation_after_confirmed_payment"
                or fact.source_ref != "owner:2026-10-21:made-to-order"
                or fact.review_status != "reviewed:owner-brief:2026-10-21" or fact.version != version
                or hashlib.sha256(fact.public_text.encode()).hexdigest() != digest):
            continue
        sentences.update(_normalize(sentence) for sentence in _claim_sentences(fact.public_text, ()))
    return frozenset(sentences)


def _unverified_deposit_refund(sentence, reply_text):
    """Ordinary refusal cannot turn the retained deposit into a refund claim."""
    deposit = re.search(r"передоплат\w*|предоплат\w*|\b(?:deposit|prepayment)\b", reply_text, re.I)
    if not deposit:
        return False
    service_review = (re.search(r"брак|дефект|помилк\w*\s+магазин\w*|ошибк\w*\s+магазин\w*|defect|store\s+error", sentence, re.I)
        and re.search(r"розгляд\w*|рассмотр\w*|перевір\w*|провер\w*|review|check", sentence, re.I))
    if service_review:
        return False
    refund_subject = re.search(r"передоплат\w*|предоплат\w*|\b(?:deposit|prepayment)\b|\b200\b|"
        r"(?:звичайн\w*|обычн\w*)[^.!?]{0,25}(?:відмов\w*|отказ\w*)|ordinary\s+refusal", sentence, re.I)
    if refund_subject:
        for match in re.finditer(r"поверта\w*|поверне\w*|возвращ\w*|верн[её]\w*|\b(?:refund\w*|return(?:ed|s)?(?:\s+(?:to\s+you|the\s+deposit))?)\b", sentence, re.I):
            if not _product_claim_negated(sentence, match.start()):
                return True
    for match in re.finditer(r"(?:лише|тільки|только|only|just)[^.!?;]{0,45}(?:достав\w*|shipping)|нічого\s+не\s+втрач\w*|ничего\s+не\s+теря\w*|lose\s+nothing", sentence, re.I):
        if not _product_claim_negated(sentence, match.start()):
            return True
    return False


def validate_reply_truth(
    reply_text: str,
    *,
    actions: Iterable[ProposedAction] = (),
    context: ReplyTruthContext,
) -> ReplyTruthResult:
    """Validate protected positive assertions against an explicit snapshot."""
    reasons: list[str] = []
    product_reason = validate_approved_product_claims(reply_text, context.approved_product_assertions,
                                                     quoted_data=context.quoted_data)
    if product_reason:
        _add(reasons, product_reason)
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

    policy_sentences = _approved_payment_policy_sentences(context) | _approved_fulfillment_policy_sentences(context)
    for sentence in _claim_sentences(reply_text, context.quoted_data):
        if _normalize(sentence) in policy_sentences:
            continue
        from management.services.ig_cooperation_policy import public_statement_topic
        if public_statement_topic(sentence, context.cooperation_policy):
            continue
        if _unverified_deposit_refund(sentence, reply_text):
            _add(reasons, "unverified_payment_policy")
        if not _shipping_claims_verified(sentence, context):
            _add(reasons, "unverified_shipping")
        if not _checkout_basket_verified(sentence, context):
            _add(reasons, "unverified_paid_basket")
        if (context.full_payment_confirmed is False and _has_positive_claim(_FULL_PAYMENT_RE, sentence)
            and (_EXPLICIT_FULL_RE.search(sentence) or not _PARTIAL_PAYMENT_RE.search(sentence))):
            _add(reasons, "unverified_payment")
        for recruitment_status in _recruitment_claims(sentence):
            if recruitment_status == "hired" or context.recruitment_status != recruitment_status:
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
            if _SHIPPING_CONTEXT_RE.search(sentence):
                _add(reasons, "unverified_shipping")
            elif pair not in ranges:
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
            shipping_money = _shipping_money_allowed(sentence, match, context)
            checkout_money = _checkout_money_allowed(sentence, match, context)
            if shipping_money is not None:
                if not shipping_money:
                    _add(reasons, "unverified_shipping")
            elif checkout_money is not None:
                if not checkout_money:
                    _add(reasons, "unverified_paid_basket")
            elif is_discount and not discount_negated:
                if amount not in discount_amounts:
                    _add(reasons, "unverified_discount")
            elif discount_negated:
                continue
            elif amount not in prices and not _agreed_amount_allowed(sentence, match, context):
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

        for timing_clause in _TIMING_CLAUSE_BOUNDARY_RE.split(sentence):
            if _timing_domain(timing_clause) == "base_arrival":
                for literal in _UNPARSED_BASE_DURATION_RE.finditer(timing_clause):
                    if not _timing_nonclaim(timing_clause, literal.start()):
                        _add(reasons, "unverified_timing")
            for start in _TIMING_START_RE.finditer(timing_clause):
                scope = _timing_scope(timing_clause, start.start())
                domain = _timing_domain(scope)
                # A comma inside 'October 12, 2027' must not erase its year
                # when the timing-domain helper isolates nearby clauses.
                if start.group(0) not in scope:
                    scope += " " + start.group(0)
                if domain and not _timing_nonclaim(timing_clause, start.start()) and not _typed_timing_allowed(
                    scope, "", domain, context, anchor_only=True):
                    _add(reasons, "unverified_timing")
            for feasibility in _DEADLINE_FEASIBILITY_RE.finditer(timing_clause):
                if not _timing_nonclaim(timing_clause, feasibility.start()) and not _typed_timing_allowed(
                    timing_clause, "", "arrival_feasibility", context,
                ):
                    _add(reasons, "unverified_timing")
            if _timing_domain(timing_clause) in {"carrier_estimate", "arrival_feasibility"}:
                for guarantee in _TIMING_GUARANTEE_RE.finditer(timing_clause):
                    if not _timing_nonclaim(timing_clause, guarantee.start()) and not _typed_timing_allowed(
                        timing_clause, "", "arrival_feasibility", context,
                    ):
                        _add(reasons, "unverified_timing")
            if not _timing_domain(timing_clause) and _DEADLINE_PROMISE_RE.search(timing_clause):
                for deadline in _NUMERIC_DEADLINE_RE.finditer(timing_clause):
                    if not _timing_nonclaim(timing_clause, deadline.start()) and not _typed_timing_allowed(
                        timing_clause, "", "arrival_feasibility", context,
                    ):
                        _add(reasons, "unverified_timing")
            if not _timing_domain(timing_clause):
                continue
            for timing_match in _TIMING_RE.finditer(timing_clause):
                if _timing_nonclaim(timing_clause, timing_match.start()):
                    continue
                timing = timing_match.group(0)
                scope = _timing_scope(timing_clause, timing_match.start())
                domain = _timing_domain(scope)
                if not (
                    _typed_timing_allowed(scope, timing, domain, context)
                    or _legacy_timing_allowed(scope, timing, context)
                    or _is_qualified_standard_dispatch_timing(scope, timing, context)
                ):
                    _add(reasons, "unverified_timing")

        pending_check = _pending_manager_stock_check(sentence)
        if pending_check:
            # The topic and its check form one semantic unit even when a
            # comma fronts the topic. Catalogue alternatives never replace
            # the customer's actual captured size, fit or colour in it.
            from management.services.ig_conversation_agreement import _configuration
            stated, ambiguity = _configuration(pending_check['subject'])
            if ambiguity:
                _add(reasons, "configuration_mismatch")
            for axis, choices in (("size", context.source_chosen_sizes),
                    ("fit", context.source_chosen_fits), ("color", context.source_chosen_colors)):
                if axis in stated and _configuration_axis_value(axis, stated[axis]) not in {
                        _configuration_axis_value(axis, value) for value in choices}:
                    _add(reasons, "configuration_mismatch")
        if (_COMPLETED_STOCK_CHECK_RE.search(sentence)
            and (_product_availability_claim(sentence) or re.search(r"склад\w*|залишк\w*|warehouse|physical\s+stock", sentence, re.I))
            and not (context.fulfillment_review_status == "reviewed" and context.availability_authorities)):
            _add(reasons, "unverified_availability")
        availability_claim = _product_availability_claim(sentence)
        reviewed_stock_claim = availability_claim
        manager_availability = _manager_availability_matches(sentence, context) if reviewed_stock_claim else ()
        if reviewed_stock_claim and not manager_availability:
            _add(reasons, "unverified_availability")
        if context.agreement_choices and _AGREEMENT_ATTRIBUTION_RE.search(sentence):
            # Natural agreement acknowledgements also use bare values such as
            # "black shirt L", without the explicit "color:" labels below.
            # Reuse the producer's pure multilingual normalization, preserving
            # its ambiguity and negation behavior.
            from management.services.ig_conversation_agreement import _configuration
            stated, ambiguous = _configuration(sentence)
            if any(code.startswith("ambiguous_") for code in ambiguous):
                _add(reasons, "configuration_mismatch")
            for axis in ("size", "fit", "color"):
                if axis not in stated:
                    continue
                expected = set()
                for row in context.agreement_choices[:80]:
                    if (isinstance(row, (tuple, list)) and len(row) == 3 and row[0] == axis
                        and isinstance(row[1], str) and type(row[2]) is int and row[2] > 0):
                        value, _ = _configuration(row[1])
                        if axis in value:
                            expected.add(value[axis])
                if stated[axis] not in expected:
                    _add(reasons, "configuration_mismatch")
                if _has_positive_claim(_CONFIGURATION_STOCK_RE, sentence) and not manager_availability:
                    _add(reasons, "unverified_availability")
        # Exact line/product identity is bound by the caller before this
        # guard. A proven price subject may describe that source choice even
        # when zero stock withholds sellable catalogue configurations.
        price_mentions = [*(_MONEY_RE.finditer(sentence)), *(_PREFIX_MONEY_RE.finditer(sentence))]
        priced_source_subject = bool(len(price_mentions) == 1
            and _decimal(price_mentions[0].group("amount")) in prices
            and re.search(r"ціна|вартіст|кошту|цена|стоим|стоит|price|cost", sentence, re.I)
            and not availability_claim and not _has_positive_claim(_CONFIGURATION_STOCK_RE, sentence)
            and _source_price_subject_matches(sentence, context))
        for axis, pattern, allowed, source_choices, audited_choices in (
            ("size", _SIZE_RE, context.allowed_sizes, context.source_chosen_sizes, context.audited_chosen_sizes),
            ("fit", _FIT_RE, context.allowed_fits, context.source_chosen_fits, ()),
            ("color", _COLOR_RE, context.allowed_colors, context.source_chosen_colors, ()),
        ):
            allowed_values = {_configuration_axis_value(axis, value) for value in allowed}
            for match in pattern.finditer(sentence):
                if _locally_negated(sentence, match.start()):
                    continue
                if _descriptive_palette_reference(sentence, match):
                    continue
                claimed = _positive_configuration_value(sentence, match)
                if not claimed:
                    continue
                # A wish belongs to its own clause, never to another stock or
                # configuration assertion in the same model sentence.
                clause_start = 0
                clause_end = len(sentence)
                for boundary in re.finditer(r"[,;]|\b(?:але|но|but|and\s+we|і\s+ми|и\s+мы)\b", sentence, re.I):
                    if boundary.end() <= match.start():
                        clause_start = boundary.end()
                    elif boundary.start() >= match.end():
                        clause_end = boundary.start()
                        break
                clause = sentence[clause_start:clause_end]
                effective = set(allowed_values)
                sentence_statuses = {fact.availability for fact in manager_availability}
                clause_manager_availability = _manager_availability_matches(clause, context,
                    expected_availability=next(iter(sentence_statuses)) if len(sentence_statuses) == 1 else None)
                if clause_manager_availability:
                    from management.services.ig_conversation_agreement import _configuration
                    given, _ = _configuration(claimed)
                    for fact in clause_manager_availability:
                        expected = {"size": fact.size, "fit": fact.fit_code, "color": fact.color_label}[axis]
                        wanted, _ = _configuration(expected)
                        if given.get(axis, _normalize(claimed)) == wanted.get(axis, _normalize(expected)):
                            effective.add(_configuration_axis_value(axis, claimed))
                if (not _has_positive_claim(_AVAILABILITY_RE, clause)
                    and not _has_positive_claim(_CONFIGURATION_STOCK_RE, clause)
                    and _AGREEMENT_ATTRIBUTION_RE.search(sentence)):
                    effective.update(_configuration_axis_value(axis, row[1]) for row in context.agreement_choices[:80]
                        if isinstance(row, (tuple, list)) and len(row) == 3 and row[0] == axis
                        and isinstance(row[1], str) and type(row[2]) is int and row[2] > 0)
                if (not _has_positive_claim(_AVAILABILITY_RE, clause)
                    and not _has_positive_claim(_CONFIGURATION_STOCK_RE, clause)
                    and (_CUSTOMER_CHOICE_RE.search(clause)
                        or _comma_choice_continuation(sentence, clause_start, clause_end))):
                    effective.update(_configuration_axis_value(axis, value) for value in source_choices)
                if priced_source_subject:
                    effective.update(_configuration_axis_value(axis, value) for value in source_choices)
                if _source_owned_availability_check(clause) or pending_check:
                    # The validated whole pending check owns its fronted
                    # subject across its comma; it cannot absorb a new claim.
                    # Catalogue metadata cannot replace that exact request.
                    effective = {_configuration_axis_value(axis, value) for value in source_choices}
                if (not _has_positive_claim(_AVAILABILITY_RE, clause)
                    and not _has_positive_claim(_CONFIGURATION_STOCK_RE, clause)
                    and CORRECTED_REQUIREMENT_RE.search(clause)):
                    effective.update(_configuration_axis_value(axis, value) for value in audited_choices)
                if _configuration_axis_value(axis, claimed) not in effective:
                    _add(reasons, "configuration_mismatch")

    return ReplyTruthResult(valid=not reasons, reasons=tuple(reasons))


__all__ = [
    "AuthorizedAction",
    "AvailabilityAuthority",
    "ProposedAction",
    "REASON_CODES",
    "ReplyTruthContext",
    "ReplyTruthResult",
    "TimingAuthority",
    "FulfillmentDeadlineAuthority",
    "CheckoutLineAuthority",
    "CheckoutOrderAuthority",
    "ShippingAuthority",
    "validate_reply_truth",
]
