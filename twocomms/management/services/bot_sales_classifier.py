"""Deterministic sales-signal classifier for the Instagram Direct bot.

This is a lightweight pre/post processor around Gemini. It must be cheap enough
to run for every inbound and manager echo message, and conservative enough not
to invent product/price facts.
"""
from __future__ import annotations

import logging
import re
from decimal import Decimal
from typing import Iterable

from django.db.models import Q
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from management.models import (
    IgClient,
    IgConversationAnalysisSnapshot,
    IgConversationSignal,
    IgDeal,
    InstagramBotMessage,
)
from management.services.ig_funnel_reset import current_message_floor

ANALYSIS_RULES_VERSION = "2026-08-03.v7"
logger = logging.getLogger(__name__)


# Маркери навмисно розширені після прод-інциденту 02.08.2026. Клієнт написав
# «Дай нове посилання, будь ласка» — жодного слова зі старого набору UK_HINTS
# там немає, апострофних і/ї/є теж, тому рахунок був 0:0, і функція повертала
# «ru» як дефолт. Це і зробило українськомовну людину «російськомовною».
# Слова, спільні для обох мов («хочу», «футболк»), лишились в обох наборах
# свідомо: вони нейтральні й мають гаситись взаємно, а не тягнути в один бік.
UK_HINTS = (
    "ціна", "скільки", "розмір", "подарунок", "передоплат", "наклад", "відправ",
    "дякую", "хочу", "можна", "собі", "друк", "футболк",
    "посилання", "будь ласка", "нове", "нового", "ще", "або", "інш", "дуже",
    "гарн", "добре", "також", "щоб", "що", "як", "де", "коли", "чому", "чи",
    "маєте", "хотіла", "хотів", "треба", "потрібно", "давайте", "зараз",
    "замовлен", "оплат", "вартіст", "кольор", "працює", "надішл", "підкаж",
    "привіт", "вітаю", "доброго", "так", "цього", "цей", "мені", "вам",
)
RU_HINTS = (
    "цена", "сколько", "размер", "подарок", "предоплат", "налож", "отправ",
    "спасибо", "хочу", "можно", "себе", "печать", "футболк",
    "ссылк", "пожалуйста", "новую", "новое", "еще", "ещё", "или", "друг",
    "очень", "хорош", "тоже", "чтобы", "что", "как", "где", "когда", "почему",
    "есть", "хотела", "хотел", "надо", "нужно", "давайте", "сейчас",
    "заказ", "оплат", "стоимост", "цвет", "работает", "пришл", "подскаж",
    "привет", "здравствуй", "добрый", "этого", "этот", "мне", "вам",
)
EN_HINTS = (
    "hello", "hi", "greetings", "please", "thanks", "thank", "order",
    "status", "delivery", "deliver", "ship", "tracking", "return", "refund",
    "exchange", "collaboration", "partnership", "price", "cost", "want",
    "need", "help", "what", "how", "where", "when", "can", "could", "yes",
    "no", "good", "afternoon", "morning", "evening", "show", "interested",
    "available", "buy",
)
URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.I)
NO_BUY_RE = re.compile(
    r"\b(?:не\s+буду\s+(?:брати|брать|купувати|покупать|замовляти|заказывать)|"
    r"не\s+хочу\s+(?:купувати|покупать|замовляти|заказывать)|"
    r"(?:брати|брать|купувати|покупать|замовляти|заказывать)\s+не\s+(?:буду|хочу)|"
    r"відмовляюсь\s+від\s+(?:покупки|замовлення)|"
    r"отказываюсь\s+от\s+(?:покупки|заказа))\b",
    re.I,
)
OPT_OUT_RE = re.compile(
    r"(?:\b(?:не\s+(?:пиш(?:и|іть|ите)(?:\s+мені|\s+мне)?|"
    r"надсилайте|присылайте|відправляйте|отправляйте)|"
    r"(?:мені|мне)\s+не\s+(?:потрібно|нужно)\s+(?:більше\s+|больше\s+)?(?:писати|писать)|"
    r"(?:мене|меня)\s+не\s+(?:цікавить|интересует)\s+(?:ця\s+|эта\s+)?(?:розсилка|рассылка)|"
    r"не\s+хочу\s+(?:більше\s+|больше\s+)?(?:отримувати|получать)\s+(?:повідомлення|сообщения|розсилку|рассылку)|"
    r"відпишіть\s+мене|отпишите\s+меня|відписатися|отписаться|"
    r"приберіть\s+(?:мене\s+)?з\s+розсилки|уберите\s+(?:меня\s+)?из\s+рассылки|"
    r"unsubscribe|(?:do\s+not|don['’]?t)\s+(?:message|contact)\s+me(?:\s+again)?|"
    r"i\s+(?:do\s+not|don['’]?t)\s+want\s+(?:any\s+)?more\s+messages)\b|"
    r"^\s*stop\s*$|\bstop\s+(?:messaging(?:\s+me)?|sending\s+(?:me\s+)?messages)\b)",
    re.I,
)


def is_explicit_opt_out(text: str) -> bool:
    """Return deterministic consent truth without CRM or provider side effects."""
    return bool(OPT_OUT_RE.search(str(text or "")))
THINKING_RE = re.compile(r"\b(подумаю|подумаємо|думаю|подума|позже|пізніше|потом)\b", re.I)
# F-PAT-001 #5: «думаю візьму L» — это решение, а не сомнение. THINKING_RE
# перетирал возражение и ставил follow-up на 12 часов вместо 2, то есть готовый
# купить получал задержку и ярлык «сомневается».
PURCHASE_DECISION_RE = re.compile(
    r"\b(візьму|беру|買|заберу|оформ(?:ляй|люйте|ляйте|ити|ляю)\w*|"
    r"давайте|давай|беремо|берем|хочу\s+замовити|хочу\s+заказать|"
    r"i(?:\s+will|\'ll)\s+take|i\s+take\s+it)\b",
    re.I,
)
DEFER_RE = re.compile(
    r"\b(не\s+зараз|не\s+сейчас|пізніше|позже|подумаю|подумаємо|потом|"
    r"немає\s+(?:мого\s+)?(?:розміру|кольору)|нет\s+(?:моего\s+)?(?:размера|цвета))\b",
    re.I,
)
PRICE_RE = re.compile(
    r"\b(дорого\w*|дорогувато|цена|ціна|ціни|цены|сколько|скільки|price|cost|"
    r"how\s+much|вартість|вартості)\b",
    re.I,
)
# «Дорого» — это возражение. «Скільки коштує» — это вопрос, и он превращается
# в возражение только когда речь о цене товара, а не о стоимости доставки
# (F-PAT-001 #1). Полное разделение вопроса и возражения — задача IMP-057.
HARD_PRICE_OBJECTION_RE = re.compile(
    r"\b(дорого\w*|дорогувато|задорого|не\s+по\s+кишені|expensive|too\s+much)\b",
    re.I,
)
# F-PAT-002: закрывающий `\b` после корня требовал границы слова, поэтому
# «передоплата», «наложкою», «накладний» не матчились вообще. На проде это дало
# `objection=prepayment` у 0 из 289 клиентов и 0 сигналов PREPAYMENT_OBJECTION
# из 989. Корень без `\w*` совпадает только с самим корнем как отдельным словом,
# а такой формы в живой речи не бывает.
PREPAY_RE = re.compile(
    r"\b(предоплат\w*|передоплат\w*|налож\w*|наклад\w*|післяплат\w*|"
    r"без\s+пред\w*|без\s+перед\w*)\b",
    re.I,
)
# F-PAT-001 #2: односимвольные альтернативы `s|m|l` с `\b` превращали «it's ok»
# в вопрос о размере (апостроф — не-словный символ, «it's» распадается на «it»
# и «s»). Однобуквенный токен сам по себе размером больше не считается: он
# осмыслен только рядом со словом «розмір/size», а это уже покрыто SIZE_WORD_RE.
SIZE_WORD_RE = re.compile(
    r"\b(размер\w*|розмір\w*|size|sizes|size\s+guide|fit|сітка|сетка|"
    r"oversize|оверсайз|regular|регуляр)\b",
    re.I,
)
SIZE_VALUE_RE = re.compile(r"\b(xs|xl|xxl|xxxl|2xl|3xl)\b", re.I)
SIZE_RE = re.compile(
    r"\b(размер\w*|розмір\w*|size|sizes|size\s+guide|fit|сітка|сетка|"
    r"oversize|оверсайз|regular|регуляр|xs|xl|xxl|xxxl|2xl|3xl)\b",
    re.I,
)
CUSTOM_REQUEST_RE = re.compile(
    r"(?:\b(?:кастом(?:н\w*)?|custom)(?:\s+(?:принт\w*|дизайн\w*))?\b|"
    r"\b(?:св(?:ой|ій)|власн\w*|мо[йяє]|мій)\s+(?:принт\w*|дизайн\w*)\b|"
    r"\b(?:зроб(?:іть|ити)|сдел(?:айте|ать)|надрук\w*|напечат\w*|нанес\w*|"
    # F-PAT-001 #4: «замінити принт на свій» — это запрос кастома. Здесь знали
    # «змінити», но не «замінити», поэтому текст доставался EXCHANGE_RE и
    # открывал постпродажный кейс обмена товара.
    r"замін(?:ити|іть|ювати)|замен(?:ить|ите|ять)|"
    r"змін(?:ити|іть|ювати)|измен(?:ить|ите))\b.{0,80}\b"
    r"(?:принт\w*|дизайн\w*|зображенн\w*|изображен\w*)\b|"
    r"\b(?:принт\w*|дизайн\w*|зображенн\w*|изображен\w*)\b.{0,80}\b"
    r"(?:зроб(?:іть|ити)|сдел(?:айте|ать)|надрук\w*|напечат\w*|нанес\w*|"
    r"замін(?:ити|іть|ювати)|замен(?:ить|ите|ять)|"
    r"змін(?:ити|іть|ювати)|измен(?:ить|ите))\b)",
    re.I,
)
PRODUCT_RE = re.compile(
    r"\b(товар\w*|футболк\w*|худі|худи|лонгслів\w*|одяг\w*|одежд\w*|"
    r"колекц\w*|модель\w*|термохром\w*|product\w*|t-?shirt\w*|shirt\w*|"
    r"hoodie\w*|longsleeve\w*|clothing)\b",
    re.I,
)
PAYMENT_RE = re.compile(r"\b(оплат\w*|платеж\w*|платіж\w*|payment|pay|checkout|invoice|ссылка|посилання|линк|лінк|link|card|карта|monobank|монобанк)\b", re.I)
PAYMENT_LINK_REFUSAL_RE = re.compile(
    r"(?:\b(?:посилання|лінк\w*|ссылк\w*|link)\b.{0,24}"
    r"\bне\s+(?:нужн\w*|потрібн\w*|треба|надо)\b|"
    r"\bне\s+(?:нужн\w*|потрібн\w*|треба|надо|хочу|буду)\b.{0,24}"
    r"\b(?:посилання|лінк\w*|ссылк\w*|link)\b|"
    r"\bне\s+(?:присыл\w*|надсила\w*|відправля\w*|отправля\w*)\b.{0,24}"
    r"\b(?:посилання|лінк\w*|ссылк\w*|link)\b|"
    r"\b(?:do\s+not|don't|dont)\s+(?:need|want|send)\b.{0,24}\blink\b|"
    r"\bno\s+(?:payment\s+)?link\b)",
    re.I,
)
# F-PAT-002, та же ошибка: «доставка», «відправка», «відділення» не матчились.
# `intent=delivery` — 0 из 289 клиентов прода.
DELIVERY_RE = re.compile(
    r"\b(достав\w*|відправ\w*|отправ\w*|delivery|deliver\w*|ship\w*|tracking|"
    r"nova\s+poshta|нова\s+пошта|новая\s+почта|нп|branch|відділен\w*|отделен\w*)\b",
    re.I,
)
ORDER_STATUS_RE = re.compile(
    r"(?:\b(?:order|замовлен\w*|заказ\w*)\b.{0,80}"
    r"\b(?:status|where|when|tracking|delivery|deliver\w*|ship\w*|статус|де|где|коли|когда|достав\w*|відправ\w*|отправ\w*)\b|"
    r"\b(?:status|where|when|tracking|статус|де|где|коли|когда)\b.{0,80}"
    r"\b(?:order|замовлен\w*|заказ\w*)\b|"
    r"\bTWC[A-Z0-9-]{5,30}\b)",
    re.I,
)
GIFT_RE = re.compile(r"\b(подарок|подарунок|на\s+подар|в\s+подар)\b", re.I)
SELF_RE = re.compile(r"\b(себе|собі|для\s+себя|для\s+себе)\b", re.I)
PHONE_RE = re.compile(r"(?:\+?38)?0\d{9}")
# F-PAT-001 #8: PHONE_RE стоял в одном `elif` с PAYMENT_RE, поэтому любой номер
# в тексте давал intent=payment и +40 к готовности — включая «мій друг
# 0501234567 казав». Номер считается контактными данными только когда клиент
# отдаёт его как свой или прямо просит оформить.
CONTACT_HANDOVER_RE = re.compile(
    r"\b(?:м(?:ій|ой)\s+(?:номер|телефон)|мо[ії]\s+дан(?:і|ные)|"
    r"мої\s+контакт\w*|тел(?:\.|ефон)?\s*:|номер\s*:|"
    r"оформ(?:ляй|люйте|ляйте|ити|ляю|ить|ите)\w*|записуйте|запишіть|"
    r"надсилайте\s+на|my\s+(?:number|phone))\b",
    re.I,
)
THIRD_PARTY_RE = re.compile(
    r"\b(?:друг|подруг\w*|знайом\w*|знаком\w*|брат|сестр\w*|колег\w*|"
    r"хлопець|дівчина|чоловік|жінка|друзі|friend)\b",
    re.I,
)

# A request for the brand's contact channel is a different fact from a
# customer handing over their own phone number.  Keep this deliberately narrow
# so order/TTN numbers and ordinary contact data do not trigger the policy.
BUSINESS_PHONE_REQUEST_RE = re.compile(
    r"(?:"
    r"\b(?:який|какой|what(?:'s|\s+is))\b.{0,32}\b(?:у\s+вас|у\s+тебя|"
    r"ваш\w*|your|business)\b.{0,24}\b(?:номер\w*|телефон\w*|"
    r"phone|number|contact)\b|"
    r"\b(?:ваш\w*|your|business)\b.{0,24}\b(?:номер\w*|телефон\w*|"
    r"phone|number|contact)\b|"
    r"\b(?:підкаж(?:іть|и)|подскаж(?:ите|и)|дай(?:те)?|дайте|скинь(?:те)?|"
    r"надішл(?:іть|и)|пришл(?:ите|и)|можна|можно|send|get|share|"
    r"can\s+i\s+(?:have|get)|could\s+i\s+(?:have|get))\b.{0,32}\b"
    r"(?:телефон\w*|phone|contact|номер\s+(?:телефон\w*|для\s+зв['’]?язку|"
    r"для\s+звонка)|number)\b"
    r"|\b(?:do\s+you\s+have|is\s+there)\b.{0,24}\b(?:a\s+)?"
    r"(?:phone|telephone|contact)\s*(?:number|details?)?\b"
    r"|\b(?:could|can|would)\s+you\b.{0,24}\b"
    r"(?:give|share|send|provide|tell)\b.{0,16}\b(?:your\s+)?"
    r"(?:phone|telephone|contact)\s*(?:number|details?)?\b"
    r"|\bhow\s+(?:can|do)\s+i\s+(?:reach|contact)\s+(?:you|the\s+business)\b"
    r"(?:.{0,16}\b(?:by\s+)?(?:phone|telephone)\b)?"
    r")",
    re.I,
)
_ORDER_NUMBER_CONTEXT_RE = re.compile(
    r"\b(?:номер|number)\b.{0,16}\b(?:замовлен\w*|заказ\w*|order|"
    r"посилк\w*|посылк\w*|ттн|ttn|tracking)\b",
    re.I,
)
PHONE_CONTACT_POLICY_KEY = "_phone_contact_policy"
PHONE_CONTACT_POLICY_SCHEMA_VERSION = 1
PHONE_CONTACT_POLICY_MAX_AGE = 15 * 60


def _has_authorized_service_case(client) -> bool:
    """Return true only for a manager-approved, still durable service case."""
    if not client or not getattr(client, "pk", None):
        return False
    try:
        from management.ig_bot_models import IgPostSaleCase

        return IgPostSaleCase.objects.filter(
            client_id=client.pk,
            status=IgPostSaleCase.Status.APPROVED,
        ).exists()
    except Exception:
        return False


def _current_phone_policy_decision(client, *, source_message_id=None) -> str:
    """Return the current-turn contact policy without retaining customer text."""
    context = getattr(client, "sales_context", None)
    if not isinstance(context, dict):
        return ""
    policy = context.get(PHONE_CONTACT_POLICY_KEY)
    if (
        not isinstance(policy, dict)
        or policy.get("schema_version") != PHONE_CONTACT_POLICY_SCHEMA_VERSION
    ):
        return ""
    try:
        if int(policy.get("source_message_id") or 0) != int(source_message_id or 0):
            return ""
        observed_at = parse_datetime(str(policy.get("observed_at") or ""))
        if observed_at is None:
            return ""
        if timezone.is_naive(observed_at):
            observed_at = timezone.make_aware(observed_at)
        age = (timezone.now() - observed_at).total_seconds()
        if not 0 <= age <= PHONE_CONTACT_POLICY_MAX_AGE:
            return ""
    except (TypeError, ValueError, OverflowError):
        return ""
    decision = str(policy.get("decision") or "")
    return decision if decision in {
        "clarify_purpose",
        "collaboration_callback",
        "support_escalation",
    } else ""


def phone_disclosure_fallback(client, *, decision: str = "") -> str:
    """Return a safe answer when a model tries to disclose a phone number."""
    language = str(getattr(client, "language", "") or "").lower()
    if decision == "support_escalation":
        if language.startswith("ru"):
            return "Передам вопрос по заказу менеджеру, чтобы помочь правильно."
        if language.startswith("en"):
            return "I will pass your order question to a manager so we can help properly."
        return "Передам питання щодо замовлення менеджеру, щоб допомогти правильно."
    if decision == "collaboration_callback":
        if language.startswith("ru"):
            return "Коротко опишите предложение, и менеджер вернётся с подходящим способом связи."
        if language.startswith("en"):
            return "Please briefly describe the proposal, and a manager will come back through the right channel."
        return "Коротко опишіть пропозицію, і менеджер повернеться з доречним способом зв'язку."
    if language.startswith("ru"):
        return "Подскажите, пожалуйста, для чего нужен контакт, и я помогу по делу."
    if language.startswith("en"):
        return "Could you tell me what you need the contact for? I will point you in the right direction."
    return "Підкажіть, будь ласка, для чого потрібен контакт, і я допоможу по суті."


def enforce_phone_disclosure_policy(
    client: IgClient,
    reply: str,
    *,
    source_message_id=None,
) -> tuple[str, bool, str]:
    """Fail closed before delivery when a model generates a phone number."""
    value = str(reply or "")
    decision = _current_phone_policy_decision(
        client,
        source_message_id=source_message_id,
    )
    if not re.search(r"(?<!\d)(?:\+?38[\s().-]*)?0(?:[\s().-]*\d){9}(?!\d)", value):
        return value, False, decision
    # No typed, manager-approved brand phone channel exists in the CRM. A
    # support policy authorizes only the manager handoff, never a model-made
    # number, so this stays fail-closed until such a channel is modeled.
    return phone_disclosure_fallback(client, decision=decision), True, decision


def phone_is_contact_handover(text: str) -> bool:
    """Whether a phone number in this message is the client's own contact data."""
    value = str(text or "")
    if not PHONE_RE.search(value):
        return False
    if THIRD_PARTY_RE.search(value) and not CONTACT_HANDOVER_RE.search(value):
        return False
    return bool(CONTACT_HANDOVER_RE.search(value))


def phone_contact_policy(
    client: IgClient,
    text: str,
    *,
    role: str = "",
    confirmed_purchase: bool | None = None,
    post_sale_request: str = "",
) -> dict:
    """Return a typed, current-turn rule for requests for the brand's phone.

    This is an instruction for the reply prompt, not a customer-facing script
    and not a replacement for intent/objection classification.  Empty output
    means that this turn does not ask for the brand's contact channel.
    """
    if role == InstagramBotMessage.Role.MANAGER:
        return {}
    value = str(text or "")
    if not BUSINESS_PHONE_REQUEST_RE.search(value):
        return {}
    if _ORDER_NUMBER_CONTEXT_RE.search(value) and not re.search(
        r"(?:телефон\w*|phone|contact|номер\s+телефон\w*)", value, re.I
    ):
        return {}

    service_evidence = bool(SUPPORT_RE.search(value) or post_sale_request)
    delivery_issue = bool(ORDER_STATUS_RE.search(value) and DELIVERY_RE.search(value))
    if confirmed_purchase is None and (service_evidence or delivery_issue):
        from management.services.bot_payment_truth import client_has_confirmed_purchase

        confirmed_purchase = client_has_confirmed_purchase(client)
    authorized_service = bool(confirmed_purchase)
    if not authorized_service and (service_evidence or delivery_issue):
        authorized_service = _has_authorized_service_case(client)
    if (service_evidence or delivery_issue) and authorized_service:
        decision = "support_escalation"
    elif COLLAB_RE.search(value):
        decision = "collaboration_callback"
    else:
        decision = "clarify_purpose"
    return {
        "schema_version": PHONE_CONTACT_POLICY_SCHEMA_VERSION,
        "decision": decision,
        "source": "deterministic_rule",
        "observed_at": timezone.now().isoformat(),
    }


QTY_RE = re.compile(r"\b(?:x|х|×)?\s*(\d{1,2})\s*(?:шт|штук|pcs|од)\b", re.I)
SIZE_TOKEN_RE = re.compile(r"\b(xs|s|m|l|xl|xxl|xxxl|2xl|3xl)\b", re.I)
# F-PAT-003: клиенты пишут размер кириллицей чаще, чем латиницей (46 токенов
# против 43 на 1113 живых сообщений). 41 сообщение содержит только кириллический
# размер, это 31 клиент, и у 24 из них `current_size` был пуст.
CYRILLIC_SIZE_MAP = {
    "хс": "XS",
    "с": "S",
    "м": "M",
    "л": "L",
    "хл": "XL",
    "ххл": "XXL",
    "хххл": "XXXL",
    "2хл": "2XL",
    "3хл": "3XL",
}
SIZE_TOKEN_CYRILLIC_RE = re.compile(
    r"\b(хххл|ххл|3хл|2хл|хл|хс|с|м|л)\b", re.I
)


LANGUAGE_SWITCH_VOTES = 2
LANGUAGE_VOTE_WINDOW = 3


def _sticky_language(client, detected: str) -> str:
    """Resolve the conversation language with hysteresis.

    Without it a single «ок, спасибо» from a Ukrainian-speaking customer flipped
    the whole conversation to Russian, and the next Ukrainian word flipped it
    back. A switch now needs to be confirmed by a second recent detection; the
    very first detection still applies immediately, because there is nothing to
    be sticky about yet.
    """
    current = client.language or ""
    requested = _requested_language(client)
    if not detected:
        return requested or current or "uk"
    if requested:
        # Явне прохання не скасовується статистикою наступних реплік: людина
        # може написати «ок» латиницею, і це не згода повернутись на попередню
        # мову. Скасувати може лише нове прохання.
        _record_language_vote(client, detected)
        return requested
    if not current:
        return detected
    if detected == current:
        _record_language_vote(client, detected)
        return current
    votes = _record_language_vote(client, detected)
    if votes.count(detected) >= LANGUAGE_SWITCH_VOTES:
        return detected
    return current


def _requested_language(client) -> str:
    context = client.sales_context if isinstance(client.sales_context, dict) else {}
    request = context.get(LANGUAGE_REQUEST_CONTEXT_KEY)
    if not isinstance(request, dict):
        return ""
    code = str(request.get("language") or "").strip().lower()
    return code if code in {"uk", "ru", "en"} else ""


LANGUAGE_REQUEST_CONTEXT_KEY = "language_request"

# Прохання про мову формулюють по-різному: прямо («пиши українською»), питанням
# («чому ти російською?»), скаргою («я не розумію російської»). Спільне в них
# одне — назва мови поруч зі словом про спілкування. Розпізнаємо саму назву
# мови, а не заготовлену фразу.
_LANGUAGE_NAME_PATTERNS = (
    ("uk", re.compile(
        r"(?:українськ\w*|укра[иї]нск\w*|ukrainian|українською|укр\.?\b)",
        re.I,
    )),
    ("ru", re.compile(
        r"(?:російськ\w*|русск\w*|russian|російською)",
        re.I,
    )),
    ("en", re.compile(r"(?:англійськ\w*|английск\w*|english)", re.I)),
)
# Форми, які самі по собі означають «мова спілкування», без слова про мовлення:
# «давай по-русски», «пиши українською», «in english please».
_LANGUAGE_SELF_SUFFICIENT_PATTERNS = (
    ("uk", re.compile(r"(?:по-українськи|українською|по-украински)", re.I)),
    ("ru", re.compile(r"(?:по-русски|російською|по-російськи)", re.I)),
    ("en", re.compile(r"(?:in\s+english|по-англійськи|по-английски)", re.I)),
)
# Контекст «це про мову спілкування», а не про напис на футболці.
_LANGUAGE_TOPIC_RE = re.compile(
    r"(?:говор\w*|розмовля\w*|разговарива\w*|пиш\w*|писа\w*|відповіда\w*|"
    r"отвеча\w*|перекл\w*|перевед\w*|спілку\w*|общат\w*|speak|write|reply|"
    r"answer|translat\w*|мов\w*|язык\w*|розумі\w*|понима\w*)",
    re.I,
)
# «Чому ти російською?» — прохання перейти на іншу мову, а не на названу.
_LANGUAGE_COMPLAINT_RE = re.compile(
    r"(?:чому|чого|why|зачем|зачім|нащо|навіщо|почему|не\s+розумію|не\s+понимаю|"
    r"don'?t\s+understand)",
    re.I,
)


def detect_language_request(text: str) -> str:
    """Мова, якою клієнт прямо просить із ним говорити, або пустий рядок.

    Скарга («чому ти російською?») трактується як прохання перейти на **іншу**
    мову: людина не просить продовжувати тією, на яку скаржиться.
    """
    value = str(text or "")
    if not value.strip():
        return ""
    self_sufficient = [
        code for code, pattern in _LANGUAGE_SELF_SUFFICIENT_PATTERNS if pattern.search(value)
    ]
    if not _LANGUAGE_TOPIC_RE.search(value) and not self_sufficient:
        return ""
    named = [code for code, pattern in _LANGUAGE_NAME_PATTERNS if pattern.search(value)]
    if not named and self_sufficient:
        named = self_sufficient
    if not named:
        return ""
    if len(named) > 1:
        # «перекладай з російської на українську» — просять цільову, тобто останню.
        return named[-1]
    code = named[0]
    if _LANGUAGE_COMPLAINT_RE.search(value):
        detected = detect_language(value)
        if detected and detected != code:
            return detected
        # Мова скарги збігається з тією, на яку скаржаться (або невизначена):
        # для російської найімовірніша цільова — українська.
        return "uk" if code == "ru" else code
    return code


def _record_language_request(client, language: str, *, message=None) -> None:
    context = client.sales_context if isinstance(client.sales_context, dict) else {}
    from django.utils import timezone as _tz

    context[LANGUAGE_REQUEST_CONTEXT_KEY] = {
        "language": language,
        "message_id": getattr(message, "pk", None),
        "observed_at": _tz.now().isoformat(),
    }
    client.sales_context = context


def _record_language_vote(client, detected: str) -> list[str]:
    context = client.sales_context if isinstance(client.sales_context, dict) else {}
    votes = context.get("_lang_votes")
    if not isinstance(votes, list):
        votes = []
    votes.append(detected)
    votes = votes[-LANGUAGE_VOTE_WINDOW:]
    context["_lang_votes"] = votes
    client.sales_context = context
    return votes


def normalize_size_token(value: str) -> str:
    """Bring a size to the single latin spelling used everywhere else.

    Every ``sales_context["size"]`` value on production is latin, so a cyrillic
    token must not be stored as-is: two spellings of one size would silently
    split the same customer choice into two.
    """
    token = str(value or "").strip().lower()
    if not token:
        return ""
    return CYRILLIC_SIZE_MAP.get(token, token.upper())


def extract_size_tokens(text: str) -> list[str]:
    """Sizes mentioned in one message, normalized, latin and cyrillic alike.

    A single cyrillic letter is accepted only with context — a short answer to a
    size question or the word «розмір» nearby. Without that guard this would
    reintroduce the «it's ok» defect (F-PAT-001 #2) in the other alphabet:
    «а с чого починається» would become size S.
    """
    value = str(text or "")
    tokens = [normalize_size_token(match) for match in SIZE_TOKEN_RE.findall(value)]
    stripped = value.strip()
    context_allows_single_letter = bool(
        len(stripped) <= 24 or SIZE_WORD_RE.search(value)
    )
    for match in SIZE_TOKEN_CYRILLIC_RE.findall(value):
        token = match.lower()
        if len(token) == 1 and not context_allows_single_letter:
            continue
        normalized = normalize_size_token(token)
        if normalized and normalized not in tokens:
            tokens.append(normalized)
    return tokens
COLLAB_RE = re.compile(
    r"\b(коллаб\w*|колаб\w*|collab\w*|cooperat\w*|partnership\w*|creator|креатор|блогер\w*|інфлюенсер\w*|"
    r"инфлюенсер\w*|партнерств\w*|співпрац\w*|сотруднич\w*|постачальник\w*|"
    r"поставщик\w*|supplier\w*|sponsor\w*|influencer\w*)\b",
    re.I,
)
WHOLESALE_RE = re.compile(
    r"(?:\b(опт\w*|оптов\w*|wholesale|b2b|дропшип\w*|тираж\w*|партію|партия)\b|"
    r"\b(?:для|в)\s+(?:магазин\w*|бутик\w*))",
    re.I,
)
SUPPORT_RE = re.compile(
    r"(?:\b(?:скарг\w*|жалоб\w*|проблем\w*|problem\w*|issue\w*|damaged|wrong|"
    r"refund\w*|return\w*|exchange\w*|брак\w*|поверн\w*|обмін\w*|обмен\w*|"
    r"верн(?:іть|ите)|пошкодж\w*|підтримк\w*|поддержк\w*)\b|"
    r"\b(?:товар\w*|замовлен\w*|заказ\w*|посилк\w*|посылк\w*)\s+не\s+"
    r"(?:прийш(?:ов|ла|ло|ли)|приш(?:ёл|ел|ла|ло|ли)|доставлен\w*)\b|"
    r"\bне\s+(?:прийш(?:ов|ла|ло|ли)|приш(?:ёл|ел|ла|ло|ли)|достав(?:или|лено))\s+"
    r"(?:товар\w*|замовлен\w*|заказ\w*|посилк\w*|посылк\w*)\b|"
    r"\bне\s+(?:отримав|отримала|отримали|получил|получила|получили)\s+"
    r"(?:товар\w*|замовлен\w*|заказ\w*|посилк\w*|посылк\w*)\b|"
    r"\b(?:товар\w*|замовлен\w*|заказ\w*|посилк\w*|посылк\w*)\s+не\s+"
    r"(?:отриман\w*|получен\w*)\b|"
    r"\b(?:my\s+)?(?:order|parcel|package)\s+"
    r"(?:has\s+not|hasn't|did\s+not|didn't|never)\s+"
    r"(?:arriv\w*|come|been\s+deliver\w*)\b|"
    r"\b(?:i\s+)?(?:have\s+not|haven't|did\s+not|didn't)\s+"
    r"receiv\w*\s+(?:my\s+)?(?:order|parcel|package)\b|"
    r"\b(?:принт\w*|друк\w*|печать)\s+"
    r"(?:не\s+то[йяе]|не\s+такий|інш\w*|друг\w*)\b)",
    re.I,
)
COMMUNITY_RE = re.compile(
    r"\b(мем\w*|прикол\w*|прикольно|круто|топчик|класно|классно|ахаха|смішн\w*|смешн\w*)\b",
    re.I,
)
COLOR_WORDS = {
    "чорн": "black",
    "черн": "black",
    "білий": "white",
    "бел": "white",
    "олив": "olive",
    "хакі": "khaki",
    "хаки": "khaki",
    "сір": "gray",
    "сер": "gray",
}
REACTION_MARKS = ("🔥", "❤", "👍", "👏", "😍", "😂", "🥰", "🙌", "💯", "🙏", "✨", "😊")


def is_reaction_only(text: str) -> bool:
    value = (text or "").strip()
    return bool(
        value
        and len(value) <= 24
        and not any(char.isalnum() for char in value)
        and any(mark in value for mark in REACTION_MARKS)
    )


def is_explicit_custom_print_request(text: str) -> bool:
    """Return true only for explicit manufacturing or design-change intent."""
    value = str(text or "")
    return bool(
        CUSTOM_REQUEST_RE.search(value)
        and not SUPPORT_RE.search(value)
        and not COLLAB_RE.search(value)
    )


def _contains_any(text: str, terms: Iterable[str]) -> int:
    low = text.lower()
    return sum(1 for term in terms if term in low)


def detect_language(text: str) -> str:
    """Мова повідомлення або пустий рядок, коли впевненості немає.

    Пустий рядок — важливий результат, а не невдача. Раніше остання строка була
    ``return "uk" if any(ch in low for ch in "іїєґ") else "ru"``, тобто будь-який
    кириличний текст без апострофних літер оголошувався російським. «Дай нове
    посилання, будь ласка» → ru. Далі це значення підхоплював гістерезис
    ``_sticky_language`` і **закріплював** його: голоси ru приходили щоразу, а uk
    не набирав двох підряд. На проді у клієнта #2 так і лежало
    ``_lang_votes=['ru','ru','uk']`` при українськомовній переписці.

    Тепер невизначеність не вигадує відповідь: збережена мова діалогу лишається
    як була, а промпт окремо показує моделі, чим клієнт користується насправді.
    """
    low = URL_RE.sub(" ", (text or "").lower())
    has_cyrillic = bool(re.search(r"[а-яёіїєґ]", low))
    if re.search(r"[a-z]", low) and not has_cyrillic:
        words = re.findall(r"[a-z]+", low)
        if any(word in EN_HINTS for word in words):
            return "en"
        return ""
    if not has_cyrillic:
        return ""
    # Літери, яких немає в російській абетці, — однозначний сигнал.
    if re.search(r"[їєґ]", low) or "'" in low and re.search(r"[а-я]'[а-я]", low):
        return "uk"
    # «і» окремо: у російській її немає, але вона часто трапляється в латинських
    # вкрапленнях і транслітерації, тому вимагаємо кириличного оточення.
    if re.search(r"(?:^|[^a-z])і(?:[^a-z]|$)", low) and re.search(r"[а-яіїєґ]", low):
        return "uk"
    # Літери, яких немає в українській абетці.
    if re.search(r"[ыъэё]", low):
        return "ru"
    uk = _contains_any(low, UK_HINTS)
    ru = _contains_any(low, RU_HINTS)
    if uk > ru:
        return "uk"
    if ru > uk:
        return "ru"
    return ""


def _signal(client, signal_type: str, *, message=None, confidence: float = 0.9, value: str = "", payload=None):
    message_obj = message if isinstance(message, InstagramBotMessage) else None
    fields = {
        "client": client,
        "message": message_obj,
        "signal_type": signal_type,
        "value": (value or "")[:255],
    }
    defaults = {
        "confidence": Decimal(str(confidence)),
        "payload": payload or {},
    }
    if message_obj is not None:
        signal, _created = IgConversationSignal.objects.get_or_create(
            **fields,
            defaults=defaults,
        )
        return signal
    return IgConversationSignal.objects.create(**fields, **defaults)


# Явная таблица приоритетов вместо порядка строк в каскаде `elif` (F-PAT-001).
# Порядок сохраняет ранее существовавшее поведение каскада, но теперь его можно
# прочитать в одном месте, оспорить и покрыть тестом.
INTENT_PRIORITY = {
    IgClient.Intent.CUSTOM_PRINT: 100,
    IgClient.Intent.SUPPORT: 90,
    IgClient.Intent.PAYMENT: 80,
    IgClient.Intent.ORDER_STATUS: 70,
    IgClient.Intent.DELIVERY: 60,
    IgClient.Intent.SIZE: 50,
    IgClient.Intent.PRICE: 40,
    IgClient.Intent.PRODUCT: 30,
}


def _resolve_readiness(
    previous: int,
    turn_score: int,
    *,
    preserve: bool = False,
    hard_zero: bool = False,
    soft_negative: bool = False,
    verified_payment: bool = False,
) -> int:
    """Resolve the compatibility score without cumulatively adding repeats."""
    previous = max(0, min(100, int(previous or 0)))
    turn_score = max(0, min(100, int(turn_score or 0)))
    if verified_payment:
        return 100
    if hard_zero:
        return 0
    if preserve:
        return previous
    if soft_negative:
        return min(35, max(turn_score, previous - 15))
    if turn_score:
        return max(turn_score, min(previous, 70))
    return max(0, previous - 10)


def _extract_context(text: str) -> dict:
    low = (text or "").lower()
    ctx: dict = {}
    qty = QTY_RE.search(low)
    if qty:
        try:
            ctx["quantity"] = max(1, min(99, int(qty.group(1))))
        except Exception:
            pass
    sizes = extract_size_tokens(low)
    if sizes:
        ctx["size"] = sizes[0]
    for stem, color in COLOR_WORDS.items():
        if stem in low:
            ctx["color"] = color
            break
    if GIFT_RE.search(low):
        ctx["gift"] = True
    if SELF_RE.search(low):
        ctx["self_purchase"] = True
    return ctx


def _record_context_provenance(
    sales_context: dict,
    context: dict,
    *,
    message=None,
    role: str = "",
    confidence: float = 0.8,
) -> dict:
    """Keep legacy flat context while recording bounded source/conflict memory."""
    if not isinstance(sales_context, dict) or not isinstance(context, dict):
        return sales_context if isinstance(sales_context, dict) else {}
    provenance = sales_context.setdefault("_provenance", {})
    if not isinstance(provenance, dict):
        provenance = {}
        sales_context["_provenance"] = provenance
    source_id = getattr(message, "pk", None)
    source_role = role or getattr(message, "role", "") or "unknown"
    observed_at = timezone.now().isoformat()
    for key, value in context.items():
        if value in (None, ""):
            continue
        previous = provenance.get(key)
        record = {
            "value": value,
            "source_message_id": source_id,
            "source_role": source_role,
            "observed_at": observed_at,
            "confidence": max(0.0, min(1.0, float(confidence))),
            "conflict": False,
        }
        if isinstance(previous, dict) and previous.get("value") != value:
            history = previous.get("history") if isinstance(previous.get("history"), list) else []
            history.append({
                "value": previous.get("value"),
                "source_message_id": previous.get("source_message_id"),
                "source_role": previous.get("source_role"),
                "observed_at": previous.get("observed_at"),
            })
            record["history"] = history[-4:]
            record["conflict"] = True
        elif isinstance(previous, dict) and isinstance(previous.get("history"), list):
            record["history"] = previous["history"][-4:]
            record["conflict"] = bool(previous.get("conflict"))
        provenance[key] = record
        sales_context[key] = value
    return sales_context


def _analysis_band(client: IgClient, result: dict) -> str:
    from management.services.bot_payment_truth import client_has_confirmed_purchase

    # CRM presentation, not provider revenue: a purchase confirmed by a manager
    # is still a purchase for the person we are talking to (IMP-013).
    if client_has_confirmed_purchase(client):
        return IgConversationAnalysisSnapshot.Band.PAID
    if result.get("interaction_type") == IgConversationAnalysisSnapshot.InteractionType.OPT_OUT:
        return IgConversationAnalysisSnapshot.Band.OPTED_OUT
    if result.get("no_buy"):
        return IgConversationAnalysisSnapshot.Band.LOST
    if client.stage == IgClient.Stage.SPAM:
        return IgConversationAnalysisSnapshot.Band.LOST
    if client.stage in {IgClient.Stage.CHECKOUT, IgClient.Stage.PAYMENT_PENDING}:
        return IgConversationAnalysisSnapshot.Band.CHECKOUT
    if IgConversationSignal.Type.CHECKOUT_STARTED in result.get("signals", []):
        return IgConversationAnalysisSnapshot.Band.HIGH_INTENT
    if int(result.get("readiness") or 0) >= 40 or client.current_product_id:
        return IgConversationAnalysisSnapshot.Band.QUALIFIED
    if result.get("signals") or client.intent != IgClient.Intent.UNKNOWN:
        return IgConversationAnalysisSnapshot.Band.EXPLORING
    return IgConversationAnalysisSnapshot.Band.COLD


_OBSERVED_FUNNEL_ORDER = [
    IgClient.Stage.NEW,
    IgClient.Stage.QUALIFYING,
    IgClient.Stage.PRODUCT_MATCHED,
    IgClient.Stage.CHECKOUT,
    IgClient.Stage.PAYMENT_PENDING,
    IgClient.Stage.PAID,
    IgClient.Stage.ORDER_CREATED,
    IgClient.Stage.DONE,
]
_OBSERVED_FUNNEL_RANK = {
    value: index for index, value in enumerate(_OBSERVED_FUNNEL_ORDER)
}


def observed_stage_target(
    current_stage: str,
    *,
    signal_types: Iterable[str] = (),
    intent: str = "",
    has_product: bool = False,
    has_size: bool = False,
    payment_pending: bool = False,
    verified_payment: bool = False,
    order_created: bool = False,
) -> str:
    """Return a monotonic evidence-backed funnel stage without reply coupling."""
    if current_stage == IgClient.Stage.SPAM:
        return current_stage
    signals = set(signal_types or ())
    signals.discard(IgConversationSignal.Type.MANAGER_TAKEOVER)
    target = IgClient.Stage.NEW
    if signals or intent not in {"", IgClient.Intent.UNKNOWN} or has_size:
        target = IgClient.Stage.QUALIFYING
    if has_product:
        target = IgClient.Stage.PRODUCT_MATCHED
    if (
        IgConversationSignal.Type.CHECKOUT_STARTED in signals
        or intent == IgClient.Intent.PAYMENT
    ):
        target = IgClient.Stage.CHECKOUT
    if payment_pending:
        target = IgClient.Stage.PAYMENT_PENDING
    if verified_payment:
        target = IgClient.Stage.PAID
    if verified_payment and order_created:
        target = IgClient.Stage.ORDER_CREATED
    current_rank = _OBSERVED_FUNNEL_RANK.get(current_stage, -1)
    target_rank = _OBSERVED_FUNNEL_RANK.get(target, -1)
    if current_stage == IgClient.Stage.COLD and not (
        verified_payment or payment_pending or order_created
    ):
        return current_stage
    if current_stage == IgClient.Stage.LEAD_TO_MANAGER and target not in {
        IgClient.Stage.CHECKOUT,
        IgClient.Stage.PAYMENT_PENDING,
        IgClient.Stage.PAID,
        IgClient.Stage.ORDER_CREATED,
        IgClient.Stage.DONE,
    }:
        return current_stage
    return target if target_rank > current_rank else current_stage


def project_observed_stage(
    client: IgClient,
    *,
    signal_types: Iterable[str] = (),
    reason: str = "observed_message",
) -> str:
    """Advance CRM stage from stored evidence even while replies are paused."""
    if not client or not getattr(client, "pk", None) or client.hidden_at:
        return getattr(client, "stage", IgClient.Stage.NEW)
    from management.services.bot_payment_truth import client_has_confirmed_purchase

    verified_payment = client_has_confirmed_purchase(client)
    deal_states = set(client.deals.values_list("status", flat=True))
    target = observed_stage_target(
        client.stage,
        signal_types=signal_types,
        intent=client.intent,
        has_product=bool(client.current_product_id),
        has_size=bool(client.current_size),
        payment_pending=IgDeal.Status.AWAITING_PAYMENT in deal_states,
        verified_payment=verified_payment,
        order_created=bool(
            IgDeal.Status.ORDER_CREATED in deal_states
            or client.deals.filter(order_id__isnull=False).exists()
        ),
    )
    if target != client.stage:
        client.set_stage(target, reason=reason)
    return target


def _aggregate_interaction_type(client: IgClient, signal_types: Iterable[str]) -> str:
    signals = set(signal_types or ())
    from management.services.bot_payment_truth import client_has_confirmed_purchase

    types = IgConversationAnalysisSnapshot.InteractionType
    if client_has_confirmed_purchase(client):
        return types.PAID_ORDER_WAITING
    if IgConversationSignal.Type.CHECKOUT_STARTED in signals:
        return types.HIGH_INTENT
    if client.intent == IgClient.Intent.PAYMENT:
        return types.HIGH_INTENT
    if IgConversationSignal.Type.CUSTOM_PRINT in signals:
        return types.CUSTOM_PRINT
    if IgConversationSignal.Type.SIZE_CONCERN in signals:
        return types.SIZE_FIT_QUESTION
    if IgConversationSignal.Type.PRICE_OBJECTION in signals:
        return types.PRICE_OBJECTION
    if IgConversationSignal.Type.PRODUCT_INTEREST in signals:
        return types.PRODUCT_INTEREST
    return types.INFORMATION_ONLY


def reconcile_rules_projection(
    client: IgClient,
    *,
    watermark: int,
) -> IgConversationAnalysisSnapshot | None:
    """Build one no-network snapshot from durable signals for visible clients."""
    if (
        not client
        or client.hidden_at
        or client.is_blocked
        or client.stage == IgClient.Stage.SPAM
    ):
        return None
    existing = client.analysis_snapshots.filter(
        analysis_model="rules",
        rules_version=ANALYSIS_RULES_VERSION,
        last_analyzed_message_id=watermark,
    ).order_by("-id").first()
    signal_types = list(dict.fromkeys(
        client.conversation_signals.filter(
            message_id__gte=current_message_floor(client),
            message_id__lte=watermark,
        )
        .exclude(signal_type=IgConversationSignal.Type.MANAGER_TAKEOVER)
        .order_by("id")
        .values_list("signal_type", flat=True)
    ))
    if existing:
        return existing
    message = client.messages.filter(pk=watermark).first()
    if not message:
        return None
    interaction_type = (
        IgConversationAnalysisSnapshot.InteractionType.MANAGER_OBSERVATION
        if message.role == InstagramBotMessage.Role.MANAGER
        else _aggregate_interaction_type(client, signal_types)
    )
    result = {
        "intent": client.intent,
        "objection": client.primary_objection,
        "readiness": client.buying_readiness,
        "signals": signal_types,
        "no_buy": client.primary_objection == IgClient.Objection.NO_BUY,
        "opt_out": bool(
            client.opted_out_at
            and (not client.opted_in_at or client.opted_in_at < client.opted_out_at)
        ),
        "interaction_type": interaction_type,
    }
    return _record_analysis_snapshot(client, message, result, role=message.role)


def post_sale_request_type(client: IgClient, text: str, *, role: str = "") -> str:
    """Return the post-sale case type this customer message asks for, or "".

    The pre-sale filter lives inside ``detect_post_sale_type``, keyed on the
    wording rather than on whether a purchase is recorded. Requiring a recorded
    purchase here would disable the mechanism on production, where order
    attribution exists for 2 clients out of 289 (DR-008).
    """
    if role == InstagramBotMessage.Role.MANAGER:
        return ""
    from management.services.ig_post_sale import post_sale_request_for_client

    return post_sale_request_for_client(client, text)


def _interaction_type(client: IgClient, result: dict, text: str, role: str) -> str:
    from management.ig_bot_models import IgPostSaleCase
    from management.services.bot_payment_truth import client_has_confirmed_purchase

    types = IgConversationAnalysisSnapshot.InteractionType
    if role == InstagramBotMessage.Role.MANAGER:
        return types.MANAGER_OBSERVATION
    if is_reaction_only(text):
        return types.REACTION_ONLY
    if result.get("opt_out"):
        return types.OPT_OUT
    if result.get("no_buy"):
        return types.EXPLICIT_NO_BUY
    # F-SCORE-002: the complaint check used to sit above the purchase check, so
    # «розмір не підійшов, хочу обмін» from a paying customer never reached
    # PAID_ORDER_WAITING and was filed under complaints. Order matters here:
    # establish the purchase first, then classify what the message asks for.
    is_buyer = client_has_confirmed_purchase(client)
    if is_buyer:
        post_sale = post_sale_request_type(client, text, role=role)
        if post_sale == IgPostSaleCase.CaseType.EXCHANGE:
            return types.EXCHANGE_REQUEST
        if post_sale == IgPostSaleCase.CaseType.RETURN:
            return types.RETURN_REQUEST
    # A real complaint stays a complaint: an undelivered parcel or a defect is
    # not a service request and must keep its own signal.
    if SUPPORT_RE.search(text or ""):
        return types.SUPPORT_COMPLAINT
    if is_buyer:
        return types.PAID_ORDER_WAITING
    if client.stage == IgClient.Stage.SPAM or client.is_blocked:
        return types.SPAM_ABUSE
    if result.get("objection") == IgClient.Objection.NO_REPLY:
        return types.NO_REPLY
    if client.stage == IgClient.Stage.PAYMENT_PENDING:
        return types.PAYMENT_PENDING
    if IgConversationSignal.Type.CHECKOUT_STARTED in result.get("signals", []):
        return types.HIGH_INTENT
    # F-PAT-001 #7: COLLAB_RE проверялся раньше, поэтому «є оптом для магазину?
    # і коллаб цікавить» становилось collaboration и оптовый лид не попадал в
    # фильтр wholesale_b2b. Опт — это выручка, коллаб — это переговоры о
    # бартере, поэтому при одновременном совпадении опт важнее.
    if WHOLESALE_RE.search(text or ""):
        return types.WHOLESALE_B2B
    if COLLAB_RE.search(text or ""):
        return types.COLLABORATION
    if result.get("intent") == IgClient.Intent.CUSTOM_PRINT:
        return types.CUSTOM_PRINT
    if result.get("intent") == IgClient.Intent.SIZE:
        return types.SIZE_FIT_QUESTION
    if result.get("objection") == IgClient.Objection.PRICE:
        return types.PRICE_OBJECTION
    if result.get("intent") == IgClient.Intent.PRODUCT:
        return types.PRODUCT_INTEREST
    if COMMUNITY_RE.search(text or ""):
        return types.COMMUNITY_CASUAL
    if (text or "").strip():
        return types.INFORMATION_ONLY
    return types.UNKNOWN


def _record_analysis_snapshot(
    client: IgClient,
    message: InstagramBotMessage | None,
    result: dict,
    *,
    role: str,
) -> IgConversationAnalysisSnapshot:
    """Persist one rules snapshot per client/message/rules watermark."""
    band = _analysis_band(client, result)
    readiness = max(0, min(100, int(result.get("readiness") or 0)))
    if band == IgConversationAnalysisSnapshot.Band.PAID:
        probability = Decimal("1.0000")
        confidence = Decimal("1.0000")
    elif band in {
        IgConversationAnalysisSnapshot.Band.LOST,
        IgConversationAnalysisSnapshot.Band.OPTED_OUT,
    }:
        probability = Decimal("0.0000")
        confidence = Decimal("0.9500")
    else:
        probability = (Decimal(readiness) / Decimal("100")).quantize(Decimal("0.0001"))
        confidence = min(
            Decimal("0.9000"),
            Decimal("0.5500") + Decimal("0.0500") * len(result.get("signals", [])),
        )

    source_role = role or getattr(message, "role", "") or "unknown"
    evidence = [{
        "source_role": source_role,
        "message_id": getattr(message, "pk", None),
        "manager_evidence": source_role == InstagramBotMessage.Role.MANAGER,
        "signals": list(result.get("signals", [])),
        "intent": result.get("intent") or IgClient.Intent.UNKNOWN,
        "objection": result.get("objection") or IgClient.Objection.NONE,
        "legacy_readiness": readiness,
    }]
    uncertainties = []
    if not client.current_product_id:
        uncertainties.append("product")
    if not client.current_size:
        uncertainties.append("size")
    if (
        result.get("intent") == IgClient.Intent.PAYMENT
        and band != IgConversationAnalysisSnapshot.Band.PAID
    ):
        uncertainties.append("payment_unverified")
    if source_role == InstagramBotMessage.Role.MANAGER:
        uncertainties.append("manager_evidence_not_customer_intent")

    message_key = getattr(message, "pk", None) or "none"
    dedupe_key = f"rules:{ANALYSIS_RULES_VERSION}:{client.pk}:{message_key}"
    snapshot, _created = IgConversationAnalysisSnapshot.objects.get_or_create(
        dedupe_key=dedupe_key,
        defaults={
            "client": client,
            "last_analyzed_message": message if isinstance(message, InstagramBotMessage) else None,
            "score_band": band,
            "interaction_type": result.get("interaction_type") or "unknown",
            "purchase_probability": probability,
            "confidence": confidence,
            "evidence": evidence,
            "uncertainties": uncertainties,
            "analysis_model": "rules",
            "rules_version": ANALYSIS_RULES_VERSION,
            "trigger": "message",
        },
    )
    return snapshot


def classify_message(
    client: IgClient,
    *,
    message: InstagramBotMessage | None = None,
    text: str | None = None,
    role: str = "",
    media_context: list[dict] | None = None,
    operational_effects: bool = True,
) -> dict:
    """Classify a single message and persist CRM state/signals.

    Returns a small dict for callers that need immediate routing decisions.
    """
    if not client:
        return {"signals": [], "readiness": 0}
    text = (text if text is not None else getattr(message, "text", "")) or ""
    low = text.lower()
    role = role or getattr(message, "role", "") or ""
    is_manager = role == InstagramBotMessage.Role.MANAGER
    reaction_only = bool(not is_manager and is_reaction_only(text))
    detected_language = detect_language(text) if text.strip() else ""
    # F-AI-008: язык перезаписывался каждым определением, поэтому у 99 из 168
    # клиентов он менялся хотя бы раз (229 переключений всего, у 93 в диалоге
    # есть и ru, и uk). Бот отвечал то на одном, то на другом. Смена требует
    # подтверждения: одно неоднозначное сообщение языком диалога не является.
    lang = (
        client.language or "uk"
        if is_manager
        else _sticky_language(client, detected_language)
    )
    if not is_manager:
        # Пряме прохання про мову сильніше і за детектор, і за гістерезис.
        # Клієнт #2 спитав «Чому ти відповідаєш російською?» — найяскравіший
        # можливий сигнал, — і система не робила з ним нічого: та сама російська
        # фраза прийшла ще раз. Тут ми лише фіксуємо факт; вибачається й
        # переходить на мову модель, своїми словами.
        requested = detect_language_request(text)
        if requested:
            lang = requested
            _record_language_request(client, requested, message=message)

    signals: list[str] = []
    # Keep durable sales context for ordinary follow-ups, but custom-print is
    # episode-scoped and must be re-earned from the current turn's evidence.
    intent = client.intent or IgClient.Intent.UNKNOWN
    objection = client.primary_objection or IgClient.Objection.NONE
    if not is_manager and not reaction_only and intent == IgClient.Intent.CUSTOM_PRINT:
        intent = IgClient.Intent.UNKNOWN
    previous_readiness = int(client.buying_readiness or 0)
    readiness = previous_readiness if is_manager or reaction_only else 0
    sales_context = dict(client.sales_context or {})

    ctx = _extract_context(text)
    if ctx:
        _record_context_provenance(
            sales_context,
            ctx,
            message=message,
            role=role,
            confidence=0.85,
        )
    if ctx.get("quantity"):
        client.current_qty = ctx["quantity"]
    if ctx.get("size"):
        client.current_size = ctx["size"][:16]
    if ctx.get("color"):
        client.current_color = ctx["color"][:64]

    # Media is an evidence axis, not a replacement for the text classifier.
    # Keep bounded provenance in sales_context so a later high-reasoning pass
    # can distinguish a product question, purchase candidate, receipt, and
    # custom reference even while replies are paused.
    media_context = [item for item in (media_context or []) if isinstance(item, dict)]
    if media_context:
        observations = sales_context.setdefault("_media_evidence", [])
        if not isinstance(observations, list):
            observations = []
            sales_context["_media_evidence"] = observations
        for item in media_context[:8]:
            observation = {
                "url": str(item.get("url") or "")[:1200],
                "role": str(item.get("role") or "other")[:32],
                "intent": str(item.get("intent") or "unknown")[:40],
                "actionable": bool(item.get("actionable")),
                "payment_evidence": bool(item.get("payment_evidence")),
                "catalog_match_allowed": bool(item.get("catalog_match_allowed")),
                "source_message_id": getattr(message, "pk", None),
                "observed_at": timezone.now().isoformat(),
            }
            # Дедуп по ассету, а не по URL целиком: подписанная ссылка Meta на
            # один и тот же файл каждый раз приходит с новой `signature`, и
            # сравнение строк плодило дубли (по одному на каждый переанализ).
            from management.bot_views import _media_asset_key

            asset_key = _media_asset_key(observation["url"])
            if observation["url"] and not any(
                _media_asset_key(row.get("url")) == asset_key
                for row in observations
                if isinstance(row, dict)
            ):
                observations.append(observation)
        del observations[:-40]

    def add(sig: str, *, conf: float = 0.9, value: str = ""):
        signals.append(sig)
        try:
            _signal(client, sig, message=message, confidence=conf, value=value)
        except Exception:
            pass

    if is_manager:
        add(IgConversationSignal.Type.MANAGER_TAKEOVER, conf=1.0)

    if not is_manager and (client.ad_id or client.ad_ref or client.referral_payload):
        add(IgConversationSignal.Type.AD_REPLY, conf=0.85, value=client.ad_id or client.ad_ref)

    was_opted_out = bool(
        client.opted_out_at
        and (not client.opted_in_at or client.opted_in_at < client.opted_out_at)
    )
    message_event_at = (
        getattr(message, "provider_created_at", None)
        or getattr(message, "created_at", None)
        or timezone.now()
    )
    explicit_opt_out = bool(not is_manager and is_explicit_opt_out(low))
    opt_out = bool(
        explicit_opt_out
        and (not client.opted_in_at or client.opted_in_at < message_event_at)
        and (not client.opted_out_at or client.opted_out_at <= message_event_at)
    )
    no_buy = bool(not is_manager and NO_BUY_RE.search(low))
    if no_buy:
        objection = IgClient.Objection.NO_BUY
        client.lost_reason = "no_buy"
        add(IgConversationSignal.Type.LOST, conf=0.95, value="no_buy")
        from management.services.bot_payment_truth import client_has_confirmed_purchase

        # A buyer who declines the next offer is not a cold lead.
        if not client_has_confirmed_purchase(client):
            # Э3.2: раньше здесь стоял `except: client.stage = COLD` — прямая
            # запись стадии в обход FSM, которая ОТМЕНЯЛА бы атомарность
            # `set_stage`: событие не записалось, а стадия всё равно менялась.
            # Если переход не удался, стадия не меняется, и это видно в логе.
            from management.services.ig_funnel_fsm import apply_stage

            transition = apply_stage(
                client, IgClient.Stage.COLD, reason="no_buy", actor="classifier"
            )
            if not transition.changed and transition.refused not in {
                "same_stage",
                "regress_not_allowed",
            }:
                logger.warning(
                    "cold stage transition refused for client %s: %s",
                    client.pk,
                    transition.refused,
                )
    if opt_out:
        opted_out_at = message_event_at
        client.opted_out_at = opted_out_at
        client.opt_out_message_id = getattr(message, "pk", None)
        client.bot_paused = True
        if not was_opted_out:
            client.reply_permission_epoch = int(client.reply_permission_epoch or 0) + 1
        client.paused_reason = "opt_out"
        client.paused_at = client.paused_at or opted_out_at

    commercially_actionable = not is_manager and not reaction_only and not no_buy and not opt_out
    # Resolved once: it gates the size objection below and the readiness guard
    # at the end of this function, and each call is a database round trip.
    from management.services.bot_payment_truth import client_has_confirmed_purchase

    confirmed_purchase = client_has_confirmed_purchase(client)
    post_sale_request = post_sale_request_type(client, text, role=role)
    phone_policy = {}
    if not is_manager:
        # Contact-channel safety is per user turn, including an opt-out or a
        # non-commercial message. It must not inherit stale policy from the
        # previous commercially actionable turn.
        phone_policy = phone_contact_policy(
            client,
            text,
            role=role,
            confirmed_purchase=confirmed_purchase,
            post_sale_request=post_sale_request,
        )
        if phone_policy:
            phone_policy["source_message_id"] = getattr(message, "pk", None)
            sales_context[PHONE_CONTACT_POLICY_KEY] = phone_policy
        else:
            # The rule is current-turn state.  Do not let a previous request
            # for the business number steer a later, unrelated reply.
            sales_context.pop(PHONE_CONTACT_POLICY_KEY, None)
    media_intents = {str(item.get("intent") or "") for item in media_context}
    media_roles = {str(item.get("role") or "") for item in media_context}
    if commercially_actionable and "custom_print_request" in media_intents:
        intent = IgClient.Intent.CUSTOM_PRINT
        readiness += 25
        add(IgConversationSignal.Type.CUSTOM_PRINT, conf=0.85, value="media")
    elif commercially_actionable and "payment_evidence" in media_intents:
        # A receipt is a manager-review signal only; provider truth remains
        # pending until the payment ledger confirms it.
        intent = IgClient.Intent.PAYMENT
        readiness += 30
        add(IgConversationSignal.Type.CHECKOUT_STARTED, conf=0.8, value="media_receipt")
    elif commercially_actionable and "product" in media_roles:
        intent = IgClient.Intent.PRODUCT
        readiness += 25 if "purchase_candidate" in media_intents else 10
        add(IgConversationSignal.Type.PRODUCT_INTEREST, conf=0.85, value="media")
    # F-PAT-001: раньше это была цепочка `elif` с первым совпадением, поэтому
    # приоритет был зашит в порядок строк. Добавление паттерна непредсказуемо
    # меняло результат для соседнего, и текстовая ветка безусловно перетирала
    # intent, установленный по медиа. Теперь собираем все сработавшие признаки
    # и выбираем по явной таблице INTENT_PRIORITY.
    if commercially_actionable:
        candidates: list[tuple[str, int, tuple | None]] = []
        if (
            is_explicit_custom_print_request(low)
            and "custom_print_request" not in media_intents
        ):
            candidates.append((
                IgClient.Intent.CUSTOM_PRINT,
                30,
                (IgConversationSignal.Type.CUSTOM_PRINT, 0.9, ""),
            ))
        if SUPPORT_RE.search(text):
            candidates.append((IgClient.Intent.SUPPORT, 0, None))
        if "payment_evidence" not in media_intents and (
            (PAYMENT_RE.search(low) and not PAYMENT_LINK_REFUSAL_RE.search(low))
            or (
                PURCHASE_DECISION_RE.search(low)
                and not DEFER_RE.search(low)
            )
            or phone_is_contact_handover(text)
        ):
            candidates.append((
                IgClient.Intent.PAYMENT,
                40,
                (IgConversationSignal.Type.CHECKOUT_STARTED, 0.8, ""),
            ))
        if ORDER_STATUS_RE.search(text):
            candidates.append((IgClient.Intent.ORDER_STATUS, 0, None))
        if DELIVERY_RE.search(low):
            candidates.append((IgClient.Intent.DELIVERY, 0, None))
        if SIZE_RE.search(low):
            candidates.append((IgClient.Intent.SIZE, 20, None))
        if PRICE_RE.search(low):
            candidates.append((IgClient.Intent.PRICE, 20, None))
        if PRODUCT_RE.search(low) and "product" not in media_roles:
            candidates.append((
                IgClient.Intent.PRODUCT,
                10,
                (IgConversationSignal.Type.PRODUCT_INTEREST, 0.75, ""),
            ))
        if candidates:
            winner = max(
                candidates, key=lambda row: INTENT_PRIORITY.get(row[0], 0)
            )
            # Медиа уже могло дать более сильный intent (например кастом-принт по
            # присланному референсу). Текст его не понижает.
            if INTENT_PRIORITY.get(winner[0], 0) >= INTENT_PRIORITY.get(intent, 0):
                intent = winner[0]
                readiness += winner[1]
                if winner[2]:
                    signal_type, conf, value = winner[2]
                    add(signal_type, conf=conf, value=value)

    # F-PAT-001 #1: блок возражений — независимый `if`, поэтому «Скільки коштує
    # доставка?» одновременно получал intent=delivery и objection=price, а
    # playbook — теги price/discount. Бот предлагал скидку на вопрос о стоимости
    # доставки. Вопрос о цене доставки ценовым возражением не является.
    hard_price = bool(HARD_PRICE_OBJECTION_RE.search(low))
    # IMP-057: a product price question is commercial intent, not an objection.
    # Only explicit negative affordability/value wording may open PRICE.
    price_objection = hard_price
    if not is_manager and not no_buy and not opt_out and price_objection:
        objection = IgClient.Objection.PRICE
        readiness += 12
        add(IgConversationSignal.Type.PRICE_OBJECTION, conf=0.85)
    if not is_manager and not no_buy and not opt_out and PREPAY_RE.search(low):
        objection = IgClient.Objection.PREPAYMENT
        readiness += 10
        add(IgConversationSignal.Type.PREPAYMENT_OBJECTION, conf=0.9)
    if not is_manager and not no_buy and not opt_out and SIZE_RE.search(low):
        # F-SCORE-006: «розмір не підійшов, хочу обмін» is not an objection to
        # buying. The purchase already happened, so filing it under objections
        # put a completed sale into the «Заперечення клієнтів» table.
        if objection == IgClient.Objection.NONE and not post_sale_request:
            objection = IgClient.Objection.SIZE
        readiness += 8
        add(IgConversationSignal.Type.SIZE_CONCERN, conf=0.8)
    if (
        not is_manager
        and not no_buy
        and not opt_out
        and THINKING_RE.search(low)
        # F-PAT-001 #5: «думаю візьму L» — решение, не сомнение. Ярлык THINKING
        # ставил follow-up на 12 часов вместо 2 и подавал в промпт тег
        # «сомневается» готовому купить.
        and not (
            PURCHASE_DECISION_RE.search(low)
            and not DEFER_RE.search(low)
        )
    ):
        objection = IgClient.Objection.THINKING
        readiness = max(readiness, 25)
        add(IgConversationSignal.Type.THINKING_OBJECTION, conf=0.9)
    if not is_manager and not no_buy and not opt_out and GIFT_RE.search(low):
        add(IgConversationSignal.Type.GIFT, conf=0.85)
        readiness += 10
    if not is_manager and not no_buy and not opt_out and SELF_RE.search(low):
        add(IgConversationSignal.Type.SELF_PURCHASE, conf=0.75)
        readiness += 8

    readiness = _resolve_readiness(
        previous_readiness,
        readiness,
        # Opt-out is a communication decision, not proof that the commercial
        # opportunity disappeared. Explicit no-buy is the hard negative axis.
        preserve=is_manager or reaction_only or (opt_out and not no_buy),
        hard_zero=no_buy,
        soft_negative=bool(DEFER_RE.search(low)) and not bool(
            IgConversationSignal.Type.CHECKOUT_STARTED in signals
        ),
        # F-SCORE-004: six polite messages after a purchase used to decay the
        # score to zero, because the only guard read provider truth.
        verified_payment=confirmed_purchase,
    )
    client.language = lang
    client.intent = intent
    client.primary_objection = objection
    client.buying_readiness = readiness
    client.sales_context = sales_context
    fields = [
        "language",
        "intent",
        "primary_objection",
        "buying_readiness",
        "lost_reason",
        "current_size",
        "current_color",
        "current_qty",
        "sales_context",
        "updated_at",
    ]
    if opt_out:
        fields.extend([
            "opted_out_at", "opt_out_message_id", "bot_paused",
            "reply_permission_epoch", "paused_reason", "paused_at",
        ])
    if is_manager:
        if (
            not client.last_manager_message_at
            or client.last_manager_message_at < message_event_at
        ):
            client.last_manager_message_at = message_event_at
            fields.append("last_manager_message_at")
    try:
        client.save(update_fields=fields)
    except Exception:
        client.save()
    result = {
        "language": lang,
        "intent": intent,
        "objection": objection,
        "readiness": readiness,
        "signals": signals,
        "no_buy": no_buy,
        "opt_out": opt_out,
        "sales_context": sales_context,
        "media_context": media_context,
        "phone_contact_policy": phone_policy,
    }
    if isinstance(message, InstagramBotMessage) and not is_manager and not reaction_only:
        try:
            from management.services.ig_objections import (
                detect_objection_types,
                observe_inbound_objection,
                observe_inbound_progress,
            )

            lifecycle_types = detect_objection_types(text)
            for lifecycle_type in lifecycle_types:
                observe_inbound_objection(
                    client,
                    message,
                    lifecycle_type,
                    readiness=readiness,
                    readiness_before=previous_readiness,
                )
            observe_inbound_progress(
                client,
                message,
                objection_types=lifecycle_types,
                readiness=readiness,
                commercial_progress=(
                    IgConversationSignal.Type.CHECKOUT_STARTED in signals
                ),
                purchase_progress=confirmed_purchase,
                abandoned=no_buy or opt_out,
            )
            result["objection_lifecycle_types"] = lifecycle_types
            result["objection_lifecycle_type"] = (
                lifecycle_types[0] if lifecycle_types else ""
            )
        except Exception as exc:
            logger.exception("Objection lifecycle projection failed: %s", exc)
            result["objection_lifecycle_types"] = []
            result["objection_lifecycle_type"] = ""
    project_observed_stage(
        client,
        signal_types=signals,
        reason=f"observed_{role or 'message'}",
    )
    result["interaction_type"] = _interaction_type(client, result, text, role)
    try:
        snapshot = _record_analysis_snapshot(client, message, result, role=role)
        result["analysis_snapshot_id"] = snapshot.pk
    except Exception:
        result["analysis_snapshot_id"] = None
    if operational_effects and isinstance(message, InstagramBotMessage) and not client.hidden_at:
        try:
            from management.services.ig_post_sale import open_post_sale_case

            result["post_sale_case_id"] = getattr(
                open_post_sale_case(client, message), "pk", None
            )
        except Exception:
            result["post_sale_case_id"] = None
        if operational_effects and message.role == InstagramBotMessage.Role.USER:
            try:
                from management.services.ig_payment_review import create_payment_review

                review = create_payment_review(client, watermark=message.pk)
                evidence = (
                    review.evidence
                    if review and isinstance(review.evidence, dict)
                    else {}
                )
                catalog_match = (
                    evidence.get("catalog_match")
                    if isinstance(evidence.get("catalog_match"), dict)
                    else {}
                )
                if catalog_match.get("status") == "matched":
                    match = {
                        "product_id": catalog_match.get("product_id"),
                        "confidence": catalog_match.get("confidence", 0),
                    }
                    sales_context["_media_catalog_match"] = {
                        "product_id": catalog_match.get("product_id"),
                        "title": str(catalog_match.get("title") or "")[:255],
                        "confidence": catalog_match.get("confidence", 0),
                        "source_message_ids": (
                            catalog_match.get("source_message_ids") or [message.pk]
                        ),
                        "observed_at": timezone.now().isoformat(),
                    }
                    client.sales_context = sales_context
                    client.save(update_fields=["sales_context", "updated_at"])
                    try:
                        from management.services import bot_orders

                        if any(
                            item.get("role") == "product"
                            and item.get("intent") == "purchase_candidate"
                            and item.get("catalog_match_allowed") is True
                            for item in media_context
                        ):
                            bot_orders.pin_product(client, match["product_id"])
                    except Exception:
                        pass
            except Exception:
                # Payment review is an operational alert; it must never block
                # message persistence or the reply boundary.
                pass
    if operational_effects and isinstance(message, InstagramBotMessage):
        try:
            from management.services.bot_conversation_analysis import schedule_analysis

            job = schedule_analysis(client, message)
            result["analysis_job_id"] = job.pk if job else None
        except Exception:
            result["analysis_job_id"] = None
    return result


def ensure_rule_classification(
    client: IgClient,
    message: InstagramBotMessage,
    *,
    media_context: list[dict] | None = None,
    operational_effects: bool = True,
    allow_post_sale_effects: bool = True,
) -> dict | None:
    """Run the deterministic projection once for a durable message watermark.

    Historical inbox projection may still open an idempotent exchange/return
    case, while analysis reconciliation can explicitly disable all case writes.
    """
    if not client or not message or not getattr(message, "pk", None):
        return None
    # Historical projection is read-only for payment/order automation, but a
    # genuine exchange/return message must still open its support case.
    if allow_post_sale_effects and not client.hidden_at:
        try:
            from management.services.ig_post_sale import open_post_sale_case

            open_post_sale_case(client, message)
        except Exception:
            pass
    message_event_at = message.provider_created_at or message.created_at
    explicit_opt_out = bool(
        message.role == InstagramBotMessage.Role.USER
        and is_explicit_opt_out(message.text or "")
    )
    if (
        explicit_opt_out
        and message_event_at
        and client.opted_in_at
        and client.opted_in_at >= message_event_at
    ):
        # A later audited opt-in wins over recovered historical consent text.
        # Do not let mixed wording mutate the current commercial projection.
        return None
    has_newer_projection = bool(message_event_at and (
        InstagramBotMessage.objects.filter(client_id=client.pk)
        .exclude(pk=message.pk)
        .annotate(
            message_event_at=Coalesce(
                "provider_created_at",
                "created_at",
            )
        )
        .filter(
            Q(message_event_at__gt=message_event_at)
            | Q(message_event_at=message_event_at)
        )
        .filter(
            ~Q(source="manual_refresh")
            | Q(analysis_snapshots__analysis_model="rules")
        )
        .exists()
    ))
    if has_newer_projection and not explicit_opt_out:
        return None
    if client.analysis_snapshots.filter(
        analysis_model="rules",
        rules_version=ANALYSIS_RULES_VERSION,
        last_analyzed_message_id=message.pk,
    ).exists():
        return None
    return classify_message(
        client,
        message=message,
        role=message.role,
        media_context=media_context,
        operational_effects=operational_effects,
    )
