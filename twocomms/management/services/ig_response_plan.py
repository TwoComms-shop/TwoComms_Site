"""Bounded response contract over existing source choices and catalog authority.

No ledger, provider, or mutation lives here. Customer choice is deliberately
separate from applicable configuration and from any checkout effect.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json
import re
from decimal import Decimal, InvalidOperation


CHOICE_FIELDS = ("product_id", "model_query", "size", "fit_option_code", "color", "quantity", "garment_type", "purchase_requested")
REVISION_PROVIDER_CONTROL_KINDS = frozenset({
    "product", "color_variant_id", "fit", "option", "size", "qty",
    "show_products", "catalog_link", "price_quoted", "paylink", "payment",
    "stage", "spam", "manager", "order",
})
_SELECTOR_WORDS = {
    "product": r"товар|модель|принт|product|model|print|посилання|ссылк|link",
    "size": r"розмір|размер|size",
    "fit": r"посадк|крій|крой|фасон|fit",
    "color": r"колір|кольор|цвет|color|colour",
    "quantity": r"кільк|колич|quantity|how many",
    "option": r"варіант|вариант|опці|опци|option|variant",
}
CHOICE_ALIASES = {
    "oversize": ("oversize", "оверсайз"), "classic": ("classic", "класичн", "классическ"),
    "black": ("black", "чорний", "чорна", "чорне", "чорну", "чорні", "чорного", "чорної",
              "чёрный", "чёрная", "чёрное", "чёрную", "чёрные", "чёрного", "чёрной",
              "черный", "черная", "черное", "черную", "черные", "черного", "черной"),
    "white": ("white", "білий", "біла", "біле", "білу", "білі", "білого", "білої",
              "белый", "белая", "белое", "белую", "белые", "белого", "белой"),
    "blue": ("blue", "синій", "синя", "синє", "синю", "сині", "синього", "синьої",
             "синий", "синяя", "синее", "синюю", "синие", "синего", "синей"),
    "pink": ("pink", "рожевий", "рожева", "рожеве", "рожеву", "рожеві", "рожевого", "рожевої",
             "розовый", "розовая", "розовое", "розовую", "розовые", "розового", "розовой"),
    "grey": ("grey", "gray", "сірий", "сіра", "сіре", "сіру", "сірі", "сірого", "сірої",
             "серый", "серая", "серое", "серую", "серые", "серого", "серой"),
    "green": ("green", "зелений", "зелена", "зелене", "зелену", "зелені", "зеленого", "зеленої",
              "зелёный", "зелёная", "зелёное", "зелёную", "зелёные", "зелёного", "зелёной",
              "зеленый", "зеленая", "зеленое", "зеленую", "зеленые", "зеленого", "зеленой"),
    "tshirt": ("tshirt", "t-shirt", "футболка", "футболку"),
    "hoodie": ("hoodie", "худі", "худи"),
}
_NEGATED_CHOICE_RE = re.compile(
    r"\b(?:не|ні|not|no|never|(?:do|does|did|has|have|had|is|are|was|were|ca|wo|could|would|should|must|need)n['’]t)\b", re.I,
)


def _unquoted_text(text):
    return re.sub(
        r'«[^»]*»|“[^”]*”|"[^"\n]*"|‘(?:[^’\n]|(?<=\w)’(?=\w))*’|'
        r'(?<!\w)\x27(?:[^\x27\n]|(?<=\w)\x27(?=\w))+\x27(?!\w)',
        "", str(text or ""),
    )


def _acknowledges_choice(kind, value, text, authority):
    from management.services.ig_reply_truth import _claim_sentences, _CUSTOMER_CHOICE_RE, _locally_negated
    aliases = CHOICE_ALIASES.get(value, (value,))
    unquoted = _unquoted_text(text)
    for sentence in _claim_sentences(unquoted, ()):
        for clause in re.split(r"[,;]|\b(?:але|но|but)\b", sentence, flags=re.I):
            choice = _CUSTOMER_CHOICE_RE.search(clause)
            for alias in aliases:
                match = re.search(r"(?<!\w)" + re.escape(alias) +
                    (r"\w*" if alias.endswith(("ичн", "ческ")) else r"(?!\w)"), clause, re.I)
                if not match or _locally_negated(clause, match.start()):
                    continue
                if _NEGATED_CHOICE_RE.search(clause[:match.end()]):
                    continue
                if choice and not _locally_negated(clause, choice.start()):
                    return True
                canonical = authority.get({"size": "sizes", "fit_option_code": "fits", "color": "colors"}.get(kind, ""), ())
                if value.casefold() in {str(item).casefold() for item in canonical} and re.search(
                    r"наявн|налич|доступн|available|in\s+stock|маємо|we\s+have", clause, re.I):
                    return True
    return False


def _acknowledges_product(title, text):
    from management.services.ig_reply_truth import _CUSTOMER_CHOICE_RE, _claim_sentences
    for sentence in _claim_sentences(text, ()):
        if title.casefold() not in sentence.casefold():
            continue
        if _NEGATED_CHOICE_RE.search(sentence):
            continue
        unquoted = _unquoted_text(sentence)
        if title.casefold() in unquoted.casefold() and _CUSTOMER_CHOICE_RE.search(unquoted):
            return True
        if re.match(r"(?:для\s+(?:моделі|модели)|for\s+)", sentence, re.I):
            return True
    return False


def asks_next_selector(text, selector):
    """A selector must occur in the same interrogative clause as its request."""
    pattern = _SELECTOR_WORDS.get(str(selector).split(":", 1)[0])
    if not pattern:
        return False
    text = _unquoted_text(text)
    for clause in re.findall(r"[^.!?;\n]*\?", text):
        if re.search(pattern, clause, re.I) and re.search(
            r"який|яку|яка|яке|які|котр|какой|какую|какое|какие|котор|which|what|how\s+many|"
            r"обира|обер|выб|choose|want|бажає|хочете|хотите|підкаж|уточн|скаж|please", clause, re.I):
            return True
    return False


def _requested_topics(text, parsed_topics=()):
    """Retain information requests independently from a choice in that source."""
    from management.services.ig_reply_truth import _locally_negated
    topics = {"info:" + str(topic).split(":", 1)[0] for topic in parsed_topics}
    for clause in re.findall(r"[^.!?\n]+[?]?", _unquoted_text(text)):
        presentation = bool(re.search(
            r"(?:покаж\w*|надішл\w*|пришл\w*|show|send)[^.!?\n]{0,30}(?:фото|photos?|pictures?|images?)"
            r"|^\s*(?:фото|photos?|pictures?|images?)\s*\?\s*$", clause, re.I))
        if presentation:
            topics.add("info:presentation")
        markers = list(re.finditer(r"скільки|сколько|яка|який|какой|коли|когда|where|when|how|what|чи\s|ли\s|підкаж|подскаж|\btell\b", clause, re.I))
        asking = ("?" in clause and not markers) or any(
            not _locally_negated(clause, marker.start()) and not any(
                negation.end() == len(clause[:marker.start()].rstrip())
                for negation in _NEGATED_CHOICE_RE.finditer(clause[:marker.start()].rstrip()))
            for marker in markers)
        if not asking:
            continue
        matched = presentation
        for topic, pattern in (
            ("price", r"ціна|ціну|вартість|кошту|цена|цену|стоим|стоит|price|cost|how\s+much"),
            ("dispatch_timing", r"відправ|отправ|dispatch|ship(?:ped|ping)?\b"),
            ("shipping", r"достав|посилк|посылк|отрима|получу|deliver|arrival"),
            ("recruitment", r"ваканс|праців|работ\w*|робот\w*|staff|job|hiring"),
            ("service", r"власн\w*\s+принт|св(?:о[йяю]|ій)\w*\s+(?:принт|дизайн)|custom|own\s+design|сервіс|сервис|service"),
        ):
            if re.search(pattern, clause, re.I):
                topics.add("info:" + topic)
                matched = True
        if "?" in clause and not matched:
            topics.add("info:question")
    return sorted(topics)


def _topic_covered(kind, response, plan):
    """Require a protected, canonical answer assertion or an admitted handoff.

    The full candidate has already passed truth/scope/action validation. This
    additional test never treats an arbitrary number or keyword as an answer.
    """
    from management.services.ig_reply_truth import (
        ReplyTruthContext, _claim_sentences, _MONEY_RE, _TIMING_RE,
        _is_qualified_standard_dispatch_timing, validate_reply_truth,
        _RECRUITMENT_STATUS_RE, _RECRUITMENT_UNCERTAINTY_RE,
    )
    authority = plan.authority
    if kind == "info:presentation" and (response.control.get("show_products") or response.control.get("catalog_link")):
        return True
    prices = tuple(Decimal(value) for value in authority["prices"])
    days = authority.get("standard_dispatch_days")
    context = ReplyTruthContext(authorized_prices=prices,
        approved_timing_claims=tuple(authority.get("timing_claims") or ()),
        explicitly_qualified_standard_dispatch_days=tuple(days) if days else None,
        payment_confirmed=authority["payment_confirmed"],
        recruitment_status=authority.get("recruitment_status", "unknown"))
    for clause in _claim_sentences(response.reply_text, ()):
        if "?" in clause or (kind in {"info:price", "info:dispatch_timing"} and re.search(
            r"не\s+(?:знаю|знаємо|знаем)|(?:do\s+not|don't)\s+know", clause, re.I)):
            continue
        if kind == "info:price" and plan.configuration.get("product_id") and re.search(
            r"ціна|вартість|кошту|цена|стоим|стоит|price|cost", clause, re.I):
            amounts = _MONEY_RE.findall(clause)
            if len(amounts) == 1 and validate_reply_truth(clause, context=context).valid:
                try:
                    if Decimal(amounts[0][0].replace(",", ".")) in prices:
                        return True
                except InvalidOperation:
                    pass
        elif kind == "info:dispatch_timing" and re.search(r"підготов|подготов|відправ|отправ|prepar|dispatch|ship", clause, re.I):
            if validate_reply_truth(clause, context=context).valid and any(
                _is_qualified_standard_dispatch_timing(clause, match.group(0), context)
                or match.group(0) in context.approved_timing_claims for match in _TIMING_RE.finditer(clause)):
                return True
        elif kind == "info:recruitment" and re.search(r"ваканс|staff|job|hiring", clause, re.I):
            if context.recruitment_status != "unknown" and _RECRUITMENT_STATUS_RE.search(clause) and validate_reply_truth(clause, context=context).valid:
                return True
            if context.recruitment_status == "unknown" and _RECRUITMENT_UNCERTAINTY_RE.search(clause):
                return True
    return bool(kind.startswith("info:") and response.control.get("manager"))


@dataclass(frozen=True)
class ResponsePlan:
    choices: dict
    evidence: dict
    scope: dict
    configuration: dict
    next_selector: str
    authority: dict
    obligations: tuple[dict, ...]
    source_selection: dict = field(default_factory=dict)
    readiness_snapshot: dict = field(default_factory=dict)

    @property
    def digest(self):
        return hashlib.sha256(json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True,
                                        separators=(",", ":")).encode()).hexdigest()

    def as_dict(self):
        return {"version": 1, "choices": self.choices, "evidence": self.evidence,
                "scope": self.scope, "configuration": self.configuration,
                "next_selector": self.next_selector, "authority": self.authority,
                "obligations": list(self.obligations)}

    def truth_context(self, context):
        # Source preferences authorize only acknowledgement of a wish. They
        # never widen the old catalog/proposal size/fit/color allowlists.
        return replace(context,
                       source_chosen_sizes=(str(self.choices["size"]),) if self.choices.get("size") else (),
                       source_chosen_fits=(str(self.choices["fit_option_code"]),) if self.choices.get("fit_option_code") else (),
                       source_chosen_colors=CHOICE_ALIASES.get(str(self.choices["color"]), (str(self.choices["color"]),)) if self.choices.get("color") else ())

    def prompt_guidance(self):
        return (
            "[SERVER RESPONSE PLAN]\n" + json.dumps(self.as_dict(), ensure_ascii=False, separators=(",", ":"))
            + "\nchoices are source-backed customer wishes, never stock, price, payment or order authority. "
            "Acknowledge accepted choices; do not ask for them or repeat their controls again. Ask only next_selector when needed. "
            "If applicability is unknown, do not assert that a requested size/fit/color is available. "
            "Ordinary selection uses product/size/fit/option/qty controls and never requires paylink/payment. "
            "item and objhandle controls are unsupported. Never promise an unsupported effect. "
            "Answer each current request; a size acknowledgement alone does not complete purchase."
        )

    def validate(self, response):
        for field, selector in (("size", "size"), ("fit_option_code", "fit"), ("color", "color")):
            if not self.choices.get(field) or self.next_selector == selector:
                continue
            for question in re.findall(r"[^.!?\n]*[?]", response.reply_text):
                if re.search(_SELECTOR_WORDS[selector], question, re.I) and re.search(
                    r"який|яку|какой|какую|which|what", question, re.I):
                    return "response_plan_repeated_selector"
        if "items" in response.control and not (response.control.get("paylink") or response.control.get("payment")):
            return "revision_cart_selection_unsupported"
        return ""

    def coverage(self, response, *, local=False):
        text = response.reply_text
        covered, remaining = [], []
        question = asks_next_selector(text, self.next_selector)
        checkout = bool(response.control.get("paylink") or response.control.get("payment"))
        for obligation in self.obligations:
            kind = obligation["kind"]
            value = str(self.choices.get(kind) or "")
            acknowledged = kind in {"size", "fit_option_code", "color", "quantity", "garment_type"} and value and _acknowledges_choice(kind, value, text, self.authority)
            product_acknowledged = kind == "product_id" and self.choices.get("product_id") and (
                str(response.control.get("product") or "") == str(self.choices["product_id"])
                or (self.configuration.get("product_title") and _acknowledges_product(self.configuration["product_title"], text)))
            done = bool(acknowledged or product_acknowledged or (kind == "purchase_requested" and checkout)
                        or (kind.startswith("info:") and _topic_covered(kind, response, self))
                        or (kind.startswith("withdrawal:") and asks_next_selector(text, kind.split(":", 1)[1])))
            (covered if done else remaining).append(obligation["id"])
        dependent_kinds = {"purchase_requested", "model_query"}
        if self.next_selector and not self.authority["prices"]:
            dependent_kinds.add("info:price")
        dependent_remaining = any(row["id"] in remaining and row["kind"] in dependent_kinds for row in self.obligations)
        other_remaining = any(row["id"] in remaining and row["kind"] not in dependent_kinds for row in self.obligations)
        disposition = "complete" if not remaining else (
            "waiting_on_customer" if question and dependent_remaining and not other_remaining else "recovery")
        return {"version": 1, "plan_digest": self.digest,
                "source_message_ids": sorted({row["source_message_id"] for row in self.obligations}),
                "covered": covered, "remaining": remaining, "disposition": disposition,
                "next_selector": self.next_selector if question else "", "local": bool(local)}


def build_response_plan(*, preferences, readiness, context, sources=()):
    """Pure constructor. Inputs must come from the existing scoped readers."""
    values = preferences.get("values") or {}
    watermark = max((int(row.get("message_id") or 0) for row in sources), default=0)
    original_evidence = preferences.get("evidence") or {}
    choices = {key: values[key] for key in CHOICE_FIELDS
               if key in values and isinstance(original_evidence.get(key), dict)
               and original_evidence[key].get("source_message_id")
               and (not watermark or int(original_evidence[key]["source_message_id"]) <= watermark)}
    evidence = {key: {name: original_evidence[key][name] for name in
                     ("source_message_id", "source_digest", "decision_id", "transition_id", "product_resolution", "presentation_effect_ids")
                     if name in original_evidence[key]} for key in choices}
    missing = [str(item) for item in readiness.get("missing") or []][:16]
    # A chosen but unavailable size requires availability resolution, not the
    # same size question. It is never silently recast as absent customer choice.
    unavailable = bool((readiness.get("size") or {}).get("requested_unavailable"))
    known = {"size": "size", "fit": "fit_option_code", "color": "color", "quantity": "quantity"}
    next_selector = next((item for item in missing if item != "options_unavailable"
                          and not choices.get(known.get(item, ""))), "")
    if not readiness.get("has_product"):
        next_selector = "" if choices.get("product_id") else "product"
    axis = next((row for row in (readiness.get("options") or {}).get("axes", [])
                 if "option:" + str(row.get("code") or "") == next_selector), {})
    configuration = {"product_id": (readiness.get("product") or {}).get("id"),
                     "next_selector_label": str(axis.get("label") or "")[:80],
                     "product_title": str((readiness.get("product") or {}).get("title") or "")[:240],
                     "applicability_known": bool(readiness.get("applicability_known")),
                     "missing": missing, "requested_size_unavailable": unavailable}
    authority = {"prices": [str(value) for value in context.authorized_prices][:8],
                 "price_ranges": [[str(low), str(high)] for low, high in context.authorized_price_ranges][:8],
                 "sizes": list(context.allowed_sizes)[:32], "fits": list(context.allowed_fits)[:16],
                 "colors": list(context.allowed_colors)[:16],
                 "order_created": bool(context.order_created), "payment_confirmed": bool(context.payment_confirmed),
                 "standard_dispatch_days": list(context.explicitly_qualified_standard_dispatch_days or ()),
                 "timing_claims": list(context.approved_timing_claims)[:16],
                 "recruitment_status": context.recruitment_status}
    from management.services.ig_commerce_turns import parse_turn
    current_sources = [row for row in sources[:64] if row.get("role") == "user"]
    obligations_list = []
    commerce_present = False
    for source in current_sources:
        parsed = parse_turn(str(source.get("text") or ""))
        kinds = ["fit_option_code" if key == "fit" else "quantity" if key == "qty" else key
                 for key in parsed.field_updates if key in {"size", "fit", "color", "qty", "quantity"}]
        if (evidence.get("model_query") or {}).get("source_message_id") == source["message_id"]:
            # A catalog title such as Classic may resemble a parser preference.
            # Canonical ambiguity evidence cannot manufacture that choice.
            kinds = [kind for kind in kinds if (evidence.get(kind) or {}).get("source_message_id") == source["message_id"]]
        if parsed.exact_product_id or (evidence.get("product_id") or {}).get("source_message_id") == source["message_id"]:
            kinds.append("product_id")
        if (evidence.get("model_query") or {}).get("source_message_id") == source["message_id"]:
            kinds.append("model_query")
        if parsed.purchase_requested:
            kinds.append("purchase_requested")
        if (evidence.get("garment_type") or {}).get("source_message_id") == source["message_id"]:
            kinds.append("garment_type")
        for field in parsed.preference_withdrawals:
            selector = {"fit": "fit", "size": "size", "color": "color"}.get(field)
            if selector:
                kinds.append("withdrawal:" + selector)
                if not choices.get({"fit": "fit_option_code"}.get(field, field)):
                    next_selector = selector
        if kinds:
            commerce_present = True
        topics = _requested_topics(source.get("text"), parse_turn(_unquoted_text(source.get("text"))).info_topics)
        if "info:presentation" in topics:
            commerce_present = True
        if topics:
            kinds.extend(topics)
        if not kinds:
            kinds = [] if re.fullmatch(r"\s*(?:привіт|вітаю|добрий\s+день|привет|здравствуйте|hello|hi|дякую|спасиб[оа]|thanks?|thank you|ок|okay|👍|❤|❤️)[.!\s]*", str(source.get("text") or ""), re.I) else ["unclassified"]
        for kind in kinds:
            obligations_list.append({"id": f"{source['message_id']}:{kind}", "kind": kind,
                                     "source_message_id": source["message_id"]})
    # Outside this commerce slice existing intent and delivery contracts retain
    # responsibility. Within it, an independent source never vanishes.
    obligations = tuple(obligations_list) if commerce_present else ()
    scope = {key: preferences[key] for key in ("session_id", "generation", "revision", "line_id", "active_index") if key in preferences}
    return ResponsePlan(choices, evidence, scope, configuration, next_selector, authority, obligations)


def capture_response_plan(client, *, revision=None):
    from management.models import IgCommerceSelectionSession
    from management.services.ig_commerce_projection import source_preferences_for
    from management.services.ig_checkout_readiness import selection_readiness
    from management.services.ig_reply_authority import build_reply_truth_context

    preferences = source_preferences_for(client)
    sources = (revision.bundle_snapshot or {}).get("sources", []) if revision else []
    watermark = max((int(row.get("message_id") or 0) for row in sources), default=0)
    session = IgCommerceSelectionSession.objects.filter(client_id=client.pk, open_slot=1).order_by("-generation").first()
    line = {}
    if session is not None:
        index = int(session.active_index or 0)
        lines = session.lines or []
        if 0 <= index < len(lines) and isinstance(lines[index], dict):
            line = lines[index]
    selection_newer = False
    if watermark and session is not None:
        from management.models import IgCommerceTurnDecision
        if IgCommerceTurnDecision.objects.filter(session=session, accepted=True, is_stale=False,
                source_message_id__gt=watermark, transition__to_revision__lte=session.revision).exists():
            # No historical source reader is being invented here. Omit the
            # newer selection; the existing inbound CAS will reject dispatch.
            preferences, line, selection_newer = {}, {}, True
    try:
        readiness = selection_readiness(product_id=line.get("product_id"), selection=line,
                                        size=str(line.get("size") or ""), fit=str(line.get("fit_option_code") or ""),
                                        quantity=line.get("quantity", 1), color=str(line.get("color") or ""), strict=True)
    except Exception:
        # Optional catalog/read-model failure cannot erase a source choice or
        # grant availability. This named gap has no invented next selector.
        readiness = {"has_product": bool(line.get("product_id")),
                     "product": {"id": line.get("product_id")},
                     "missing": ["options_unavailable"], "applicability_known": False}
    if selection_newer:
        from management.services.ig_reply_truth import ReplyTruthContext
        context = ReplyTruthContext()
    else:
        context = build_reply_truth_context(client)
    from management.services.ig_commerce_projection import captured_selection_from_preferences
    return replace(build_response_plan(preferences=preferences, readiness=readiness,
                                      context=context, sources=sources),
                   source_selection=captured_selection_from_preferences(client.pk, preferences),
                   readiness_snapshot=readiness)
