"""Bounded response contract over existing source choices and catalog authority.

No ledger, provider, or mutation lives here. Customer choice is deliberately
separate from applicable configuration and from any checkout effect.
"""
from __future__ import annotations
from copy import deepcopy

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
_PAYMENT_WORDS = re.compile(r"\b(?:оплат\w*|сплат\w*|сплач\w*|передоплат\w*|платіж\w*|плат[её]ж\w*|paid|payment|pay|чек\w*|квитанц\w*|receipt\w*|реквізит\w*|реквизит\w*|iban)\b", re.I)
_RECEIPT_WORDS = re.compile(r"\b(?:чек\w*|квитанц\w*|receipt\w*)\b", re.I)
_PAYMENT_CLAIM = re.compile(r"\b(?:оплатив|оплатила|оплатил|оплатили|сплатив|сплатила|сплачено|оплачено|paid)\b", re.I)
_PAYMENT_PENDING = re.compile(r"перевір\w*|провер\w*|звір\w*|свер\w*|очіку\w*|ожида\w*|pending|under\s+review|(?:not|isn['’]t)\s+(?:yet\s+)?(?:verified|confirmed)|не\s+(?:підтвердж\w*|подтвержд\w*)", re.I)
_RECEIPT_ACK = re.compile(r"(?:отрим\w*|одерж\w*|получ\w*|бач\w*|виж\w*|received|see|got)[^.!?\n]{0,45}(?:чек\w*|квитанц\w*|receipt\w*)|(?:чек\w*|квитанц\w*|receipt\w*)[^.!?\n]{0,45}(?:отрим\w*|получ\w*|received|under\s+review|перевір\w*|провер\w*)", re.I)
_REPORTED_PAYMENT_ACK = re.compile(r"(?:повідом\w*|сообщ\w*|заяв\w*|reported|say|said)[^.!?\n]{0,60}(?:оплат\w*|сплат\w*|paid|payment)|(?:заяв\w*|reported)\s+(?:оплат\w*|payment)", re.I)
_REPORTED_RECEIPT_ACK = re.compile(r"(?:повідом\w*|сообщ\w*|заяв\w*|reported|say|said)[^.!?\n]{0,60}(?:чек\w*|квитанц\w*|receipt\w*)", re.I)
_PAYMENT_FORWARDED = re.compile(r"(?:передал\w*|передав\w*|передан\w*|передано|сповіст\w*|уведомил\w*|уведомлен\w*|forwarded|notified)[^.!?\n]{0,70}(?:менедж\w*|команд\w*|manager|team)|(?:менедж\w*|manager|team)[^.!?\n]{0,45}(?:уведомлен\w*|notified|сповіщ\w*)", re.I)


def _payment_requests(text):
    """Current source semantics; questions/negations never prove a payment."""
    text = _unquoted_text(text)
    result = set()
    for clause in re.findall(r"[^,.!?;\n]+[?]?", text):
        if not _PAYMENT_WORDS.search(clause):
            continue
        asking = "?" in clause or bool(re.search(r"\b(?:як|как|де|где|чи|ли|how|where|when|what|can|could|підкаж\w*|подскаж\w*)\b", clause, re.I))
        negated = bool(_NEGATED_CHOICE_RE.search(clause))
        instructions = bool(re.search(r"посилан\w*|ссыл\w*|лінк\w*|link|реквізит\w*|реквизит\w*|iban|як\s+оплат|как\s+оплат|how\s+(?:do\s+i\s+)?pay|надішл\w*|пришл\w*|send", clause, re.I))
        asking = asking or bool(instructions and re.search(r"\b(?:дайт\w*|дайте|надішл\w*|пришл\w*|send|provide|please)\b", clause, re.I))
        future = bool(re.search(r"\b(?:буду|збира\w*|собира\w*|will|going\s+to)\b", clause, re.I))
        if not asking and not negated and not future:
            if _RECEIPT_WORDS.search(clause) and not instructions:
                result.add("payment:receipt")
            elif _PAYMENT_CLAIM.search(clause) and not instructions:
                result.add("payment:claim")
        if asking or negated:
            result.add("info:payment_instructions" if instructions else "info:payment")
    return result


def _captured_payment_observation(raw, current_sources):
    """Keep only admitted observations of exact current USER sources, no PII."""
    raw = raw if isinstance(raw, dict) else {}
    if raw.get("reason"):
        return {}
    observation = raw.get("observation", raw)
    if not isinstance(observation, dict) or observation.get("state") not in {"observed", "pending"}:
        return {}
    current_ids = {row["message_id"] for row in current_sources}
    refs = raw.get("source_refs") or []
    proven_ids = {ref.get("message_id", ref.get("id")) for ref in refs if isinstance(ref, dict)
                  and re.fullmatch(r"[a-f0-9]{64}", str(ref.get("source_digest", ref.get("digest", ""))))}
    ids = sorted(identity for identity in observation.get("source_message_ids") or []
                 if type(identity) is int and identity in current_ids and identity in proven_ids)
    if not ids:
        return {}
    receipts = []
    for row in (observation.get("receipts") or [])[:8]:
        if not isinstance(row, dict) or row.get("source_message_id") not in ids or row.get("role") != "receipt" or row.get("state") != "inspected":
            continue
        if not row.get("source_part_id") or not re.fullmatch(r"[a-f0-9]{64}", str(row.get("content_hash") or "")):
            continue
        # Only document type/transfer outcome go into the plan, never banking PII.
        facts = row.get("receipt_facts") or {}
        transfer = facts.get("payment_status")
        receipts.append({"source_message_id": row["source_message_id"], "source_part_id": str(row["source_part_id"])[:64],
            "content_hash": row["content_hash"], "document_type": "receipt",
            "reported_transfer_status": transfer if transfer in {"completed", "pending", "failed", "unknown"} else "unknown"})
    return {"state": "observed" if receipts else "pending", "source_message_ids": ids,
        "receipts": receipts, "payment_verified": False, "verification": "unresolved"}


def _payment_covered(kind, response, plan, source_id):
    # Final truth/action validation still owns authority. Coverage cannot turn
    # a keyword, size ack, manager control or OCR into proof of money/forwarding.
    if kind == "info:payment_instructions":
        return bool(response.control.get("paylink"))
    text = _unquoted_text(response.reply_text)
    from management.services.ig_reply_truth import _claim_sentences, _has_positive_claim, _locally_negated
    if plan.authority["payment_confirmed"]:
        from management.services.ig_reply_truth import _PAYMENT_CLAIM_RE
        if any("?" not in clause and _has_positive_claim(_PAYMENT_CLAIM_RE, clause)
               for clause in _claim_sentences(text, ())):
            return True
    sentences = [clause for clause in re.findall(r"[^.!?\n]+[?]?", text) if "?" not in clause]
    pending = any(_PAYMENT_WORDS.search(clause) and any(not _locally_negated(clause, match.start())
        for match in _PAYMENT_PENDING.finditer(clause)) for clause in sentences)
    receipt_ack = any(_has_positive_claim(_RECEIPT_ACK, clause) for clause in sentences)
    claim_ack = any(_has_positive_claim(_REPORTED_PAYMENT_ACK, clause) for clause in sentences)
    receipt_report_ack = any(_has_positive_claim(_REPORTED_RECEIPT_ACK, clause) for clause in sentences)
    source_receipt = source_id in plan.payment_observation.get("source_message_ids", [])
    if kind == "payment:receipt":
        return (receipt_ack if source_receipt else receipt_report_ack) and pending
    if kind == "payment:claim":
        return (claim_ack or (receipt_ack and source_receipt)) and pending
    return pending and bool(plan.payment_observation or plan.payment_claim_source_ids)


def _unquoted_text(text):
    return re.sub(
        r'«[^»]*»|“[^”]*”|"[^"\n]*"|‘(?:[^’\n]|(?<=\w)’(?=\w))*’|'
        r'(?<!\w)\x27(?:[^\x27\n]|(?<=\w)\x27(?=\w))+\x27(?!\w)',
        "", str(text or ""),
    )


def _acknowledges_choice(kind, value, text, authority, *, audited=False):
    from management.services.ig_reply_truth import _claim_sentences, _CUSTOMER_CHOICE_RE, _locally_negated, CORRECTED_REQUIREMENT_RE
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
                if audited and CORRECTED_REQUIREMENT_RE.search(clause):
                    return True
                if not audited and choice and not _locally_negated(clause, choice.start()):
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
        markers = list(re.finditer(
            r"\b(?:скільки|сколько|яка|який|какой|коли|когда|where|when|how|what|чи|ли|tell)\b"
            r"|\b(?:підкаж|подскаж)", clause, re.I))
        asking = ("?" in clause and not markers) or any(
            not _locally_negated(clause, marker.start()) and not any(
                negation.end() == len(clause[:marker.start()].rstrip())
                for negation in _NEGATED_CHOICE_RE.finditer(clause[:marker.start()].rstrip()))
            for marker in markers)
        if not asking:
            continue
        matched = presentation
        for topic, pattern in (
            ("payment", _PAYMENT_WORDS.pattern),
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
    payment_observation: dict = field(default_factory=dict)
    payment_claim_source_ids: tuple[int, ...] = ()
    payment_context_snapshot: dict = field(default_factory=dict)
    payment_context_boundary: dict = field(default_factory=dict)
    payment_context_fence: str = ""
    payment_context_reason: str = ""
    payment_context_captured_at: str = ""
    source_cart_capture: dict = field(default_factory=dict)
    line_plans: tuple = ()
    plan_gap: str = ""

    @property
    def digest(self):
        return hashlib.sha256(json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True,
                                        separators=(",", ":")).encode()).hexdigest()

    def as_dict(self):
        payload = {"version": 1, "choices": self.choices, "evidence": self.evidence,
                "scope": self.scope, "configuration": self.configuration,
                "next_selector": self.next_selector, "authority": self.authority,
                "obligations": list(self.obligations)}
        if self.payment_observation or self.payment_claim_source_ids:
            payload.update(payment_observation=self.payment_observation,
                payment_claim_source_ids=list(self.payment_claim_source_ids))
        if self.line_plans:
            payload["line_plans"] = [line.as_dict() for line in self.line_plans]
            payload["next_selector_line"] = self.next_selector_line
        if self.plan_gap:
            payload["plan_gap"] = self.plan_gap
        return payload

    @property
    def next_selector_line(self):
        if self.obligations and all(row["kind"].startswith(("payment:", "info:payment")) for row in self.obligations):
            return {}
        for line in self.line_plans:
            row = line.as_dict()
            if row["next_selector"]:
                return {"line_id": row["line_id"], "recipient_id": row["recipient_id"],
                    "field": row["next_selector"], "label": row["configuration"].get("next_selector_label") or row["next_selector"],
                    "product_title": row["configuration"].get("product_title") or "",
                    "ordinal": row["index"] + 1}
        return {}

    def line_truth_contexts(self, context):
        """Exact line contexts; callers must validate claims in that line's clause."""
        contexts = {}
        for line in self.line_plans:
            row = line.as_dict()
            authority = row["authority"]
            scoped = replace(context, authorized_prices=tuple(Decimal(value) for value in authority["prices"]),
                authorized_price_ranges=tuple((Decimal(low), Decimal(high)) for low, high in authority["price_ranges"]),
                allowed_sizes=tuple(authority["sizes"]), allowed_fits=tuple(authority["fits"]), allowed_colors=tuple(authority["colors"]))
            contexts[row["line_id"]] = ResponsePlan(row["choices"], row["evidence"], row["scope"],
                row["configuration"], row["next_selector"], authority, tuple(row["obligations"])).truth_context(scoped)
        return contexts

    def validate_multiline_claims(self, response, context):
        """Validate each assertion against its own line; no union authority."""
        if not self.line_plans:
            return ""
        from management.services.ig_response_cart_plan import matching_line_ids, scoped_claim_clauses
        from management.services.ig_reply_truth import validate_reply_truth
        scoped = self.line_truth_contexts(context)
        unbound = replace(context, authorized_prices=(), authorized_price_ranges=(), allowed_sizes=(),
            allowed_fits=(), allowed_colors=(), source_chosen_sizes=(), source_chosen_fits=(),
            source_chosen_colors=(), audited_chosen_sizes=())
        for sentence in scoped_claim_clauses(_unquoted_text(response.reply_text)):
            matches = matching_line_ids(sentence, self.line_plans)
            candidate_context = scoped[matches[0]] if len(matches) == 1 else unbound
            if len(matches) == 1:
                row = next(line.as_dict() for line in self.line_plans if line.line_id == matches[0])
                local = ResponsePlan(row["choices"], row["evidence"], row["scope"], row["configuration"],
                    row["next_selector"], row["authority"], tuple(row["obligations"]))
                from types import SimpleNamespace
                reason = local.validate(SimpleNamespace(reply_text=sentence, control={}))
                if reason:
                    return reason
            if not validate_reply_truth(sentence, context=candidate_context).valid:
                return "response_plan_line_claim_unverified"
        return ""

    def truth_context(self, context):
        # Source preferences authorize only acknowledgement of a wish. They
        # never widen the old catalog/proposal size/fit/color allowlists.
        return replace(context,
                       source_chosen_sizes=(str(self.choices["size"]),) if self.choices.get("size") and not self._audited_size() else (),
                       audited_chosen_sizes=(str(self.choices["size"]),) if self.choices.get("size") and self._audited_size() else (),
                       source_chosen_fits=(str(self.choices["fit_option_code"]),) if self.choices.get("fit_option_code") else (),
                       source_chosen_colors=CHOICE_ALIASES.get(str(self.choices["color"]), (str(self.choices["color"]),)) if self.choices.get("color") else ())

    def prompt_projection(self):
        """Compact presentation only; full plan/digest/source proof stay intact.

        No mandatory obligation or allowed claim is truncated. The existing
        required-context budget owner rejects the complete projection if it
        cannot fit, rather than admitting a partial list of customer needs.
        """
        groups = {}
        for obligation in self.obligations:
            key = (obligation.get("line_id"), obligation["source_message_id"])
            group = groups.setdefault(key, {"line_id": key[0], "source_message_id": key[1], "obligations": []})
            group["obligations"].append([obligation["id"], obligation["kind"]])
        for group in groups.values():
            prefix = (f"{group['source_message_id']}:{group['line_id']}:" if group["line_id"] is not None
                else f"{group['source_message_id']}:")
            pairs = group["obligations"]
            # Exact presentation compression only: canonical IDs are the
            # shared prefix plus the complete kind. Preserve arbitrary IDs
            # verbatim rather than guessing or normalizing their structure.
            if all(identity == prefix + kind for identity, kind in pairs):
                group["id_prefix"] = prefix
                group["obligation_kinds"] = [kind for identity, kind in pairs]
                del group["obligations"]
        payload = {"version": 1, "plan_digest": self.digest, "obligation_columns": ["id", "kind"],
            "obligation_groups": list(groups.values())}
        def choice_sources(evidence):
            grouped = {}
            for name, proof in evidence.items():
                key = (proof.get("authority", "customer_source"), proof.get("source_message_id"))
                grouped.setdefault(key, []).append(name)
            return [{"authority": authority, "source_message_id": source_id, "fields": names}
                for (authority, source_id), names in grouped.items()]
        if self.line_plans:
            payload["lines"] = []
            payload["choice_source_groups"] = []
            for line in self.line_plans:
                row = line.as_dict()
                configuration, authority = row["configuration"], row["authority"]
                item = {"line_id": row["line_id"], "recipient_id": row["recipient_id"], "ordinal": row["index"] + 1,
                    "product_id": configuration.get("product_id"), "title": configuration.get("product_title") or "",
                    "choice_source_refs": [],
                    "catalog_authority": {key: authority[key] for key in ("prices", "price_ranges", "sizes", "fits", "colors") if authority.get(key)},
                    "readiness": {"applicability_known": bool(configuration.get("applicability_known"))}}
                if configuration.get("missing"):
                    item["readiness"]["missing"] = configuration["missing"]
                for group in choice_sources(row["evidence"]):
                    if group not in payload["choice_source_groups"]:
                        payload["choice_source_groups"].append(group)
                    item["choice_source_refs"].append(payload["choice_source_groups"].index(group))
                if configuration.get("requested_size_unavailable"):
                    item["readiness"]["requested_size_unavailable"] = True
                if row["next_selector"]:
                    item["next_selector"] = row["next_selector"]
                    if configuration.get("next_selector_label"):
                        item["next_selector_label"] = configuration["next_selector_label"]
                payload["lines"].append(item)
            payload["next_selector_line"] = self.next_selector_line
            payload["authority"] = {key: value for key, value in self.authority.items()
                if key not in {"prices", "price_ranges", "sizes", "fits", "colors"}}
        else:
            payload.update(choices=deepcopy(self.choices), choice_sources=choice_sources(self.evidence),
                scope=deepcopy(self.scope), configuration=deepcopy(self.configuration),
                next_selector=self.next_selector, authority=deepcopy(self.authority))
        if self.payment_observation:
            payload["payment_observation"] = {key: deepcopy(self.payment_observation[key]) for key in
                ("state", "source_message_ids", "payment_verified", "verification") if key in self.payment_observation}
            payload["payment_observation"]["receipts"] = [{key: receipt[key] for key in
                ("source_message_id", "document_type", "reported_transfer_status") if key in receipt}
                for receipt in self.payment_observation.get("receipts", ())]
        if self.payment_claim_source_ids:
            payload["payment_claim_source_ids"] = list(self.payment_claim_source_ids)
        if self.plan_gap:
            payload["plan_gap"] = self.plan_gap
        if self.configuration.get("reorder_observations"):
            payload["reorder_resolution"] = self.configuration["reorder_observations"]
        return payload

    def prompt_guidance(self):
        audited = self._audited_size() or any(proof.get("authority") == "audited_correction"
            for line in self.line_plans for proof in line.as_dict()["evidence"].values())
        guidance = (
            "[SERVER RESPONSE PLAN]\n" + json.dumps(self.prompt_projection(), ensure_ascii=False, separators=(",", ":"))
            + "\nCurrent wishes and full field proof are in captured source-cart facts. Source choices authorize wishes only; "
            "Each obligation_kinds entry retains full kind and full ID=id_prefix+kind; other groups keep full [id,kind] pairs. "
            "unknown applicability authorizes no availability claim. Empty catalog_authority permits no catalog claim. "
            "Cover every obligation independently; size acknowledgement alone completes neither purchase nor payment support. "
            "Do not repeat confirmed choices/questions/controls. Ask only the named next selector. "
            "Selection controls product/size/fit/option/qty require no payment; item/objhandle and unsupported effects are forbidden. "
        )
        if (not self.line_plans or self.payment_observation or self.payment_claim_source_ids
            or any(item["kind"].startswith(("payment:", "info:payment")) for item in self.obligations)):
            guidance += (
            "Payment claims and receipt/OCR are reported evidence, never verified money. Acknowledge an observed receipt with "
            "verification pending; a text-only claim permits reported-payment acknowledgement, never claiming receipt was read. "
            "Only authority.payment_confirmed permits verified payment. No forwarding/manager notification claim without SENT notification proof. "
            "Conversation amounts retain their source authority and component: merchandise, delivery and payable total. "
            "Attribute seller_instruction amounts explicitly to the manager's quote (За розрахунком менеджера); "
            "attribute customer-accepted amounts to the agreement. Never call the payable total a garment price or claim delivery/payment was paid from a quote. "
            "Links/requisites are instructions; receipt response completion leaves manager verification unresolved."
            )
        if audited:
            guidance += (
                "\nAn audited manager correction is a neutral corrected requirement; never attribute its value to what the customer "
                "said/chose/requested. Original source remains unchanged; correction proves neither stock nor an updated checkout."
            )
        if self.line_plans:
            guidance += ("\nchoice_source_refs indexes choice_source_groups for that line's fields and source authority. "
                "Identify each line by title/product or ordinal; duplicate SKUs require recipient or ordinal. "
                "Sibling acknowledgement/price/availability covers nothing for another line. Historical superseded choices are not current. "
                "Ask at most next_selector_line.field for that line; ambiguous line targets require finite clarification.")
        if self.configuration.get("reorder_observations"):
            guidance += ("\nThe exact historical reorder remains unresolved. Current context describes the prior purchase; "
                "no new cart or purchase has been admitted. Clarify the exact owned order/position or the stated unavailable "
                "configuration. Never invent the latest order, promote old payment/shipping to a new purchase, or submit selection/payment controls.")
        return guidance

    def _audited_size(self):
        proof = self.evidence.get("size") or {}
        receipt = (proof.get("correction") or {}).get("receipt") or {}
        return bool(proof.get("authority") == "audited_correction"
                    and receipt.get("schema") == "manager-correction.v1"
                    and receipt.get("field") == "size"
                    and receipt.get("after") == self.choices.get("size"))

    def validate(self, response):
        if self.plan_gap:
            return self.plan_gap
        if any(row["kind"].startswith(("payment:", "info:payment")) for row in self.obligations):
            from management.services.ig_reply_truth import _claim_sentences, _has_positive_claim
            if any("?" not in clause and _has_positive_claim(_PAYMENT_FORWARDED, clause)
                   for clause in _claim_sentences(_unquoted_text(response.reply_text), ())):
                return "response_plan_payment_forwarding_unverified"
        if self.line_plans:
            from management.services.ig_response_cart_plan import matching_line_ids
            for question in re.findall(r"[^.!?\n]*[?]", _unquoted_text(response.reply_text)):
                selectors = [field for field, pattern in _SELECTOR_WORDS.items() if re.search(pattern, question, re.I)
                    and asks_next_selector(question, field)]
                if not selectors:
                    continue
                matches = matching_line_ids(question, self.line_plans)
                expected = self.next_selector_line
                if len(matches) != 1 or not expected or matches[0] != expected["line_id"] or selectors != [expected["field"].split(":", 1)[0]]:
                    return "response_plan_line_selector_mismatch"
            if "items" in response.control and not (response.control.get("paylink") or response.control.get("payment")):
                return "revision_cart_selection_unsupported"
            return ""
        if self._audited_size():
            from management.services.ig_reply_truth import _claim_sentences, _SIZE_RE, _locally_negated, CORRECTED_REQUIREMENT_RE
            chosen = self.choices.get("size")
            control = response.control.get("size")
            if control and (not chosen or str(control).casefold() != str(chosen).casefold()):
                return "response_plan_audited_size_conflict"
            if (response.control.get("paylink") or response.control.get("payment")) and not self.authority.get("audited_size_configuration_matches"):
                return "response_plan_audited_configuration_unready"
            for sentence in _claim_sentences(response.reply_text, ()):
                for clause in re.split(r"[,;]|\b(?:але|но|but)\b", sentence, flags=re.I):
                    unquoted = _unquoted_text(clause)
                    # Catalog eligibility cannot authorize a different current
                    # requirement. Quoted whole statements remain data, while
                    # a quoted size token in an assertion remains a claim.
                    values = [match.group("value") for match in _SIZE_RE.finditer(clause)
                        if not _locally_negated(clause, match.start())
                        and not _NEGATED_CHOICE_RE.fullmatch(match.group("value"))]
                    corrected = CORRECTED_REQUIREMENT_RE.search(unquoted)
                    if corrected and _locally_negated(unquoted, corrected.start()):
                        corrected = None
                    if corrected and any(not chosen or value.casefold() != str(chosen).casefold() for value in values):
                        return "response_plan_audited_size_conflict"
                    choice = re.search(r"\b(?:ви|вы|you)\s+(?:(?:have|had)\s+)?(?:обрали|вибрали|просили|сказали|написали|уточнили|попросили|выбрали|хотели|chose|chosen|selected|said|wrote|clarified|requested)\b|\b(?:ваш\w*|your)\s+(?:вибір|выбор|choice|побажан\w*|пожелан\w*|запит\w*|запрос\w*|request|preference)\b", unquoted, re.I)
                    if not choice:
                        continue
                    if _locally_negated(unquoted, choice.start()):
                        continue
                    historical = re.search(r"\b(?:earlier|previously|originally|formerly|раніше|раньше|попередньо|прежде)\b", unquoted[:choice.start()], re.I)
                    if historical:
                        continue
                    bare_size = re.search(r"\b(?:XS|S|M|L|XL|XXL|XXXL|XXXXL|[5-8]XL)\b", clause, re.I)
                    if values or bare_size or (chosen and re.search(r"\b" + re.escape(str(chosen)) + r"\b", clause, re.I)):
                        return "response_plan_audited_choice_misattributed"
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

    def coverage(self, response, *, local=False, checkout_cart_binding=None):
        if self.plan_gap:
            return {"version": 1, "plan_digest": self.digest,
                "source_message_ids": sorted({row["source_message_id"] for row in self.obligations}),
                "covered": [], "remaining": [row["id"] for row in self.obligations], "disposition": "recovery",
                "next_selector": "", "local": bool(local), "gap": self.plan_gap}
        if self.line_plans:
            return self._line_coverage(response, local=local, checkout_cart_binding=checkout_cart_binding)
        text = response.reply_text
        covered, remaining = [], []
        question = asks_next_selector(text, self.next_selector)
        checkout = bool(response.control.get("paylink") or response.control.get("payment"))
        for obligation in self.obligations:
            kind = obligation["kind"]
            value = str(self.choices.get(kind) or "")
            acknowledged = kind in {"size", "fit_option_code", "color", "quantity", "garment_type"} and value and _acknowledges_choice(kind, value, text, self.authority, audited=kind == "size" and self._audited_size())
            product_acknowledged = kind == "product_id" and self.choices.get("product_id") and (
                str(response.control.get("product") or "") == str(self.choices["product_id"])
                or (self.configuration.get("product_title") and _acknowledges_product(self.configuration["product_title"], text)))
            done = bool(acknowledged or product_acknowledged or (kind == "purchase_requested" and checkout)
                        or (kind.startswith(("payment:", "info:payment")) and _payment_covered(kind, response, self, obligation["source_message_id"]))
                        or (kind.startswith("info:") and not kind.startswith("info:payment") and _topic_covered(kind, response, self))
                        or (kind.startswith("withdrawal:") and asks_next_selector(text, kind.split(":", 1)[1])))
            (covered if done else remaining).append(obligation["id"])
        dependent_kinds = {"purchase_requested", "model_query"}
        if self.next_selector and not self.authority["prices"]:
            dependent_kinds.add("info:price")
        dependent_remaining = any(row["id"] in remaining and row["kind"] in dependent_kinds for row in self.obligations)
        other_remaining = any(row["id"] in remaining and row["kind"] not in dependent_kinds for row in self.obligations)
        disposition = "complete" if not remaining else (
            "waiting_on_customer" if question and dependent_remaining and not other_remaining else "recovery")
        result = {"version": 1, "plan_digest": self.digest,
                "source_message_ids": sorted({row["source_message_id"] for row in self.obligations}),
                "covered": covered, "remaining": remaining, "disposition": disposition,
                "next_selector": self.next_selector if question else "", "local": bool(local)}
        if (self.payment_observation or self.payment_claim_source_ids) and not self.authority["payment_confirmed"]:
            result["payment_verification"] = "unresolved"
        return result

    def _line_coverage(self, response, *, local=False, checkout_cart_binding=None):
        from types import SimpleNamespace
        from management.services.ig_response_cart_plan import (
            admitted_checkout_line_ids, line_clauses, matching_line_ids,
        )
        clauses = line_clauses(response.reply_text, self.line_plans)
        admitted = admitted_checkout_line_ids(checkout_cart_binding, self.source_cart_capture, local=local)
        covered, remaining = [], []
        rows = {line.line_id: line.as_dict() for line in self.line_plans}
        selector = self.next_selector_line
        question = any(asks_next_selector(text, selector.get("field", ""))
            and matching_line_ids(text, self.line_plans) == (selector.get("line_id"),)
            for text in re.findall(r"[^.!?\n]*[?]", _unquoted_text(response.reply_text))) if selector else False
        for obligation in self.obligations:
            kind, line_id = obligation["kind"], obligation.get("line_id")
            done = False
            if kind.startswith(("payment:", "info:payment")):
                done = _payment_covered(kind, response, self, obligation["source_message_id"])
            elif line_id in rows:
                row = rows[line_id]
                text = ". ".join(clauses[line_id])
                plan = ResponsePlan(row["choices"], row["evidence"], row["scope"], row["configuration"],
                    row["next_selector"], row["authority"], tuple(row["obligations"]))
                value = str(obligation.get("value") or "")
                if kind in {"size", "fit_option_code", "color", "quantity", "garment_type"} and value:
                    done = _acknowledges_choice(kind, value, text, row["authority"], audited=kind == "size" and plan._audited_size())
                elif kind == "product_id":
                    done = bool(row["configuration"].get("product_title") and _acknowledges_product(row["configuration"]["product_title"], text))
                elif kind.startswith("withdrawal:"):
                    done = question and selector.get("line_id") == line_id and selector.get("field") == kind.split(":", 1)[1]
                elif kind.startswith("info:"):
                    done = _topic_covered(kind, SimpleNamespace(reply_text=text, control=response.control), plan)
                elif kind == "purchase_requested":
                    done = (line_id, obligation.get("recipient_id")) in admitted
            elif kind.startswith("info:") and kind != "info:price":
                done = _topic_covered(kind, response, self)
            (covered if done else remaining).append(obligation["id"])
        dependent = {"purchase_requested", "model_query", "info:price"}
        if selector:
            dependent.add("withdrawal:" + selector["field"])
        waiting = question and not self.plan_gap and all(item["kind"] in dependent for item in self.obligations if item["id"] in remaining)
        result = {"version": 1, "plan_digest": self.digest,
            "source_message_ids": sorted({item["source_message_id"] for item in self.obligations}),
            "covered": covered, "remaining": remaining,
            "disposition": "complete" if not remaining and not self.plan_gap else "waiting_on_customer" if waiting else "recovery",
            "next_selector": selector.get("field", "") if question else "", "next_selector_line": selector if question else {},
            "local": bool(local), **({"gap": self.plan_gap} if self.plan_gap else {})}
        if (self.payment_observation or self.payment_claim_source_ids) and not self.authority["payment_confirmed"]:
            result["payment_verification"] = "unresolved"
        return result

def build_response_plan(*, preferences, readiness, context, sources=(), payment_observation=None, captured_cart=None,
                        readiness_by_line=None, contexts_by_line=None):
    """Pure constructor. Inputs must come from the existing scoped readers."""
    if captured_cart is not None:
        plan = build_response_plan(preferences=preferences, readiness=readiness, context=context)
        admitted = _captured_payment_observation(payment_observation, [row for row in sources if row.get("role") == "user"])
        claim_ids = tuple(sorted(row["message_id"] for row in sources if row.get("role") == "user" and "payment:claim" in _payment_requests(row.get("text"))))
        plan = replace(plan, payment_observation=admitted, payment_claim_source_ids=claim_ids)
        return attach_cart_response_plan(plan, capture=captured_cart, sources=sources,
            readiness_by_line=readiness_by_line or {}, contexts_by_line=contexts_by_line or {}, context=context)
    values = preferences.get("values") or {}
    watermark = max((int(row.get("message_id") or 0) for row in sources), default=0)
    original_evidence = preferences.get("evidence") or {}
    choices = {key: values[key] for key in CHOICE_FIELDS
               if key in values and isinstance(original_evidence.get(key), dict)
               and original_evidence[key].get("source_message_id")
               and (not watermark or int(original_evidence[key]["source_message_id"]) <= watermark)}
    evidence = {key: {name: original_evidence[key][name] for name in
                     ("source_message_id", "source_digest", "decision_id", "transition_id", "product_resolution", "presentation_effect_ids", "authority", "correction")
                     if name in original_evidence[key]} for key in choices}
    if "size" in (preferences.get("cleared") or {}):
        evidence["size"] = deepcopy(preferences["cleared"]["size"])
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
    current_sources = [row for row in sources[:64] if row.get("role") == "user"]
    payment_observation = _captured_payment_observation(payment_observation, current_sources)
    receipt_ids = set(payment_observation.get("source_message_ids") or [])
    payment_claim_ids = set()
    obligations_list = []
    commerce_present = False
    for source in current_sources:
        from management.services.ig_commerce_turns import parse_turn
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
        payment_kinds = _payment_requests(source.get("text"))
        if source["message_id"] in receipt_ids:
            payment_kinds.add("payment:receipt")
        if payment_kinds:
            commerce_present = True
            # Replace its generic payment topic, keeping unrelated questions.
            topics = [topic for topic in topics if not topic.startswith("info:payment")]
            kinds.extend(sorted(payment_kinds))
            if "payment:claim" in payment_kinds:
                payment_claim_ids.add(source["message_id"])
        if any(topic in topics for topic in ("info:presentation", "info:price")):
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
    if obligations and all(row["kind"].startswith(("payment:", "info:payment")) for row in obligations):
        next_selector = ""
    scope = {key: preferences[key] for key in ("session_id", "generation", "revision", "line_id", "active_index") if key in preferences}
    return ResponsePlan(choices, evidence, scope, configuration, next_selector, authority, obligations,
        payment_observation=payment_observation, payment_claim_source_ids=tuple(sorted(payment_claim_ids)))


def _capture_payment_context(client, revision, sources, session, line):
    """Read the exact sealed source boundary; optional failures are finite."""
    from copy import deepcopy
    from django.utils import timezone
    from django.utils.dateparse import parse_datetime
    from management.models import IgFunnelResetAudit
    from management.services.ig_admin_state_capture import capture_payment_context

    if revision is None or not sources:
        return {}, {}, "", "payment_context_sealed_source_unavailable", ""
    namespaces = {row.get("source_namespace") for row in sources}
    if len(namespaces) != 1 or not next(iter(namespaces)):
        return {}, {}, "", "payment_context_namespace_unavailable", ""
    clocks = []
    for row in sources:
        event = parse_datetime(str(row.get("provider_created_at") or row.get("observed_created_at") or ""))
        if event is None or timezone.is_naive(event):
            return {}, {}, "", "payment_context_event_time_unavailable", ""
        clocks.append((event, int(row["message_id"])))
    event, identity = max(clocks)
    watermark = {"message_id": identity, "event_at": event.isoformat()}
    reset = IgFunnelResetAudit.objects.filter(client_id=client.pk).order_by("-pk").values("pk", "reset_after_message_id").first() or {}
    episode = getattr(client, "current_commercial_episode", None)
    boundary = {"client_id": client.pk, "episode_id": client.current_commercial_episode_id,
        "order_id": getattr(episode, "intended_order_id", None), "line_id": str(line.get("line_id") or ""),
        "recipient_id": str(line.get("recipient_id") or "self"), "reset_floor": int(reset.get("reset_after_message_id") or 0) + 1,
        "source_namespace": next(iter(namespaces)), "reset_id": reset.get("pk"), "erasure_epoch": "",
        "source_watermark": watermark, "watermark": deepcopy(watermark)}
    if client.privacy_erasure_started_at or any(int(row["message_id"]) < boundary["reset_floor"] for row in sources):
        return {}, boundary, "", "payment_context_scope_changed", ""
    now = timezone.now()
    first, reason, fence = capture_payment_context(boundary, now=now)
    second, second_reason, second_fence = capture_payment_context(boundary, now=now)
    if not fence or reason != second_reason or fence != second_fence:
        return {}, boundary, "", "payment_context_changed", now.isoformat()
    return deepcopy(first), boundary, fence, reason, now.isoformat()
def attach_cart_response_plan(plan, *, capture, sources, readiness_by_line, contexts_by_line, context):
    """Pure attachment, preserving the legacy constructor and single-line API."""
    from management.services.ig_response_cart_plan import build_cart_response_lines, matching_line_ids, MAX_OBLIGATIONS
    def single(preferences, readiness, line_context):
        if line_context is None:
            applicable = bool(readiness.get("applicability_known"))
            line_context = replace(context, authorized_prices=(), authorized_price_ranges=(),
                allowed_sizes=tuple((readiness.get("size") or {}).get("available") or ()) if applicable else (),
                allowed_fits=tuple(item["code"] for item in (readiness.get("fit") or {}).get("options") or ()) if applicable else (),
                allowed_colors=tuple(item["name"] for item in (readiness.get("color") or {}).get("options") or ()) if applicable else (),
                source_chosen_sizes=(), source_chosen_fits=(),
                source_chosen_colors=(), audited_chosen_sizes=())
        else:
            line_context = replace(context, authorized_prices=line_context.authorized_prices,
                authorized_price_ranges=line_context.authorized_price_ranges, allowed_sizes=line_context.allowed_sizes,
                allowed_fits=line_context.allowed_fits, allowed_colors=line_context.allowed_colors,
                source_chosen_sizes=(), source_chosen_fits=(), source_chosen_colors=(), audited_chosen_sizes=())
        return build_response_plan(preferences=preferences, readiness=readiness, context=line_context)
    lines, gap = build_cart_response_lines(capture=capture, sources=sources,
        readiness_by_line=readiness_by_line, authority_by_line=contexts_by_line, single_builder=single)
    if gap:
        return replace(plan, source_cart_capture=deepcopy(capture), plan_gap=gap)
    obligations = [item for line in lines for item in line.as_dict()["obligations"]]
    for source in sources:
        if source.get("role") != "user":
            continue
        source_id = source["message_id"]
        owners = {item["line_id"] for item in obligations if item["source_message_id"] == source_id}
        accepted_owners = set(owners)
        semantic = set(matching_line_ids(_unquoted_text(source.get("text")), lines))
        if semantic:
            owners = owners & semantic if owners else semantic
        topics = _requested_topics(source.get("text"))
        payment_kinds = _payment_requests(source.get("text"))
        if source_id in plan.payment_observation.get("source_message_ids", ()):
            payment_kinds.add("payment:receipt")
        if payment_kinds:
            topics = [topic for topic in topics if not topic.startswith("info:payment")]
            obligations.extend({"id": f"{source_id}:{kind}", "kind": kind, "source_message_id": source_id} for kind in sorted(payment_kinds))
        for topic in topics:
            owner = next(iter(owners)) if len(owners) == 1 else None
            obligations.append({"id": f"{source_id}:{owner}:{topic}" if owner else f"{source_id}:{topic}",
                "kind": topic, "source_message_id": source_id, **({"line_id": owner} if owner else {})})
        # A typed operation with no current surviving source field remains debt.
        # Removed/replaced lines need a separate accepted-operation receipt.
        if not accepted_owners and not topics and not payment_kinds and source.get("text") and not re.fullmatch(
            r"\s*(?:привіт|вітаю|добрий\s+день|привет|здравствуйте|hello|hi|дякую|спасиб[оа]|thanks?|thank you|ок|okay|👍|❤|❤️)[.!\s]*", str(source["text"]), re.I):
            obligations.append({"id": f"{source_id}:unresolved_line_operation", "kind": "unresolved_line_operation", "source_message_id": source_id})
    gap = "response_plan_obligation_bound" if len(obligations) > MAX_OBLIGATIONS or any(len(item["id"]) > 160 for item in obligations) else ""
    return replace(plan, source_cart_capture=deepcopy(capture), line_plans=lines,
        obligations=tuple(obligations), plan_gap=gap,
        next_selector="" if obligations and all(item["kind"].startswith(("payment:", "info:payment")) for item in obligations) else next((line.as_dict()["next_selector"] for line in lines if line.as_dict()["next_selector"]), ""))


def _provided_source_cart(client, revision, capture):
    """Pure admission of the original artifact under a sealed source boundary.

    The caller separately owns the live owner/head/reset/order SourceCartFence.
    This adapter never substitutes a newer capture when the artifact is invalid.
    """
    from management.services.ig_turn_intelligence import (
        TurnContextError, _event, capture_digest, validate_source_cart_capture,
    )
    try:
        if not isinstance(capture, dict) or revision is None:
            raise TurnContextError("response_plan_provided_cart_invalid")
        detached = deepcopy(capture)
        bundle = revision.bundle_snapshot
        sources = bundle.get("sources") if isinstance(bundle, dict) else None
        if (revision.client_id != client.pk or getattr(revision, "erasure_started_at_snapshot", None)
            or getattr(client, "privacy_erasure_started_at", None)
            or not isinstance(sources, list) or not 1 <= len(sources) <= 32
            or capture_digest(bundle) != revision.snapshot_digest):
            raise TurnContextError("response_plan_provided_cart_revision_changed")
        if any(not isinstance(row, dict) or type(row.get("message_id")) is not int or row["message_id"] <= 0 for row in sources):
            raise TurnContextError("response_plan_provided_cart_sources_invalid")
        namespaces = {row.get("source_namespace") for row in sources}
        if len(namespaces) != 1 or not next(iter(namespaces)):
            raise TurnContextError("response_plan_provided_cart_scope_changed")
        event, source_id = max((_event(row.get("provider_created_at") or row.get("observed_created_at")), row["message_id"]) for row in sources)
        scope = detached.get("scope")
        if not isinstance(scope, dict) or not detached.get("lines"):
            raise TurnContextError("response_plan_provided_cart_invalid")
        boundary = {**scope, "client_id": client.pk, "episode_id": client.current_commercial_episode_id,
            "source_namespace": next(iter(namespaces)), "watermark": {"message_id": source_id, "event_at": event.isoformat()}}
        # Some sealed callers carry additional owner scope; retain those exact
        # values when present rather than drawing them from mutable live rows.
        sealed_scope = bundle.get("scope")
        if isinstance(sealed_scope, dict):
            boundary.update({key: sealed_scope[key] for key in ("order_id", "reset_id", "reset_floor") if key in sealed_scope})
        index = detached.get("active_index")
        rows = detached.get("lines")
        if not isinstance(rows, list) or type(index) is not int or not 0 <= index < len(rows) or not isinstance(rows[index], dict):
            raise TurnContextError("response_plan_provided_cart_invalid")
        boundary.update(line_id=rows[index].get("line_id"), recipient_id=rows[index].get("recipient_id"))
        return validate_source_cart_capture(detached, boundary), ""
    except TurnContextError as exc:
        return {}, exc.reason
    except (AttributeError, KeyError, TypeError, ValueError, RecursionError):
        return {}, "response_plan_provided_cart_invalid"


def capture_response_plan(client, *, revision=None, source_cart_capture=None):
    plan = _capture_response_plan_inputs(client, revision=revision, source_cart_capture=source_cart_capture)
    observations = ((getattr(revision, "action_receipts", None) or {}).get("commerce_reduction") or {}).get("observations") or []
    if not isinstance(observations, (list, tuple)) or len(observations) > 32 or any(not isinstance(row, dict) for row in observations):
        return replace(plan, plan_gap="reorder_observation_changed")
    pending = [row for row in observations if row.get("classification") == "reorder_clarification"]
    if not pending:
        return plan
    sources = {row["message_id"]: row for row in revision.bundle_snapshot.get("sources") or []}
    if len(pending) > 32 or any(row.get("schema") != "commerce-source-observation.v1"
        or row.get("client_id") != client.pk or row.get("source_message_id") not in sources
        or row.get("source_digest") != hashlib.sha256(str(sources[row["source_message_id"]].get("text") or "").encode()).hexdigest()
        or row.get("source_namespace") != sources[row["source_message_id"]].get("source_namespace") for row in pending):
        return replace(plan, plan_gap="reorder_observation_changed")
    return replace(plan, configuration={**plan.configuration, "reorder_observations": deepcopy(pending)})


def _capture_response_plan_inputs(client, *, revision=None, source_cart_capture=None):
    if source_cart_capture is not None:
        from management.services.ig_reply_truth import ReplyTruthContext
        cart, reason = _provided_source_cart(client, revision, source_cart_capture)
        if reason:
            plan = build_response_plan(preferences={}, readiness={}, context=ReplyTruthContext())
            return replace(plan, plan_gap=reason)
        sources = deepcopy(revision.bundle_snapshot["sources"])
        watermark = max(row["message_id"] for row in sources)
        return _capture_cart_response_plan(client, sources=sources, cart_capture=cart, watermark=watermark, revision=revision)
    from management.models import IgCommerceSelectionSession
    from management.services.ig_commerce_projection import source_preferences_for, capture_current_selection_lines
    from management.services.ig_checkout_readiness import selection_readiness
    from management.services.ig_reply_authority import build_reply_truth_context

    sources = (revision.bundle_snapshot or {}).get("sources", []) if revision else []
    watermark = max((int(row.get("message_id") or 0) for row in sources), default=0)
    cart_capture = deepcopy(capture_current_selection_lines(client.pk))
    cart_rows = cart_capture.get("lines") or []
    all_line_contract = len(cart_rows) > 1 or any(
        "operation_index" in proof for row in cart_rows for proof in (row.get("evidence") or {}).values())
    if all_line_contract:
        return _capture_cart_response_plan(client, sources=sources, cart_capture=cart_capture, watermark=watermark, revision=revision)
    preferences = source_preferences_for(client)
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
    try:
        payment_snapshot, payment_boundary, payment_fence, payment_reason, payment_at = _capture_payment_context(
            client, revision, sources, session, line)
    except Exception:
        # Optional observation capture neither fabricates receipt truth nor
        # relaxes the existing final money/source/permission guards.
        payment_snapshot, payment_boundary, payment_fence, payment_reason, payment_at = {}, {}, "", "payment_context_unavailable", ""
    slot = (payment_snapshot.get("slots") or {}).get("receipt.observation") or {}
    payment_observation = {"observation": slot.get("value") or {}, "source_refs": slot.get("source_refs") or []}
    from management.services.ig_commerce_projection import captured_selection_from_preferences
    plan = build_response_plan(preferences=preferences, readiness=readiness, context=context,
        sources=sources, payment_observation=payment_observation)
    if plan._audited_size():
        from management.services.ig_commerce_projection import _matching_legacy_selection
        legacy = _matching_legacy_selection(client)
        chosen = plan.choices.get("size")
        matches = bool(chosen and line.get("product_id") and readiness.get("has_product")
                       and readiness.get("applicability_known") and not readiness.get("missing")
                       and client.current_product_id == line.get("product_id")
                       and str(client.current_size or "").casefold() == str(chosen).casefold()
                       and (not legacy.get("size") or str(legacy["size"]).casefold() == str(chosen).casefold()))
        plan = replace(plan, authority={**plan.authority, "audited_size_configuration_matches": matches})
    return replace(plan,
                   source_selection=captured_selection_from_preferences(client.pk, preferences),
                   readiness_snapshot=readiness, source_cart_capture=cart_capture, payment_context_snapshot=payment_snapshot,
                   payment_context_boundary=payment_boundary, payment_context_fence=payment_fence,
                   payment_context_reason=payment_reason, payment_context_captured_at=payment_at)


def _capture_cart_response_plan(client, *, sources, cart_capture, watermark, revision=None):
    """One canonical source observation; subsequent reads are catalog-only."""
    from management.services.ig_response_cart_plan import source_line_preferences
    from management.services.ig_source_cart_catalog import resolve_source_cart_catalog
    from management.services.ig_reply_authority import build_reply_truth_context
    from management.services.ig_commerce_projection import captured_selection_from_preferences
    from management.services.ig_reply_truth import ReplyTruthContext
    active = cart_capture.get("active_line_id")
    catalog = resolve_source_cart_catalog(cart_capture)
    readiness_by_line, preferences_by_line = catalog.readiness_by_line, {}
    for row in cart_capture.get("lines") or []:
        preferences = source_line_preferences(cart_capture, row, source_watermark=watermark)
        preferences_by_line[row["line_id"]] = preferences
    preferences = preferences_by_line.get(active) or {}
    # A source beyond the sealed revision invalidates the complete observation.
    newer = bool(watermark and any(source_id > watermark for source_id in (cart_capture.get("fence") or {}).get("source_ids", ())))
    context = ReplyTruthContext() if newer else build_reply_truth_context(client)
    readiness = readiness_by_line.get(active) or {}
    active_row = next((row for row in cart_capture.get("lines") or [] if row["line_id"] == active), {})
    try:
        payment_snapshot, payment_boundary, payment_fence, payment_reason, payment_at = _capture_payment_context(client, revision, sources, None, active_row)
    except Exception:
        payment_snapshot, payment_boundary, payment_fence, payment_reason, payment_at = {}, {}, "", "payment_context_unavailable", ""
    slot = (payment_snapshot.get("slots") or {}).get("receipt.observation") or {}
    payment_observation = {"observation": slot.get("value") or {}, "source_refs": slot.get("source_refs") or []}
    plan = build_response_plan(preferences=preferences, readiness=readiness, context=context, sources=sources,
        payment_observation=payment_observation, captured_cart=cart_capture,
        readiness_by_line=readiness_by_line, contexts_by_line=catalog.contexts_by_line)
    if catalog.gaps and not catalog.readiness_by_line:
        plan = replace(plan, plan_gap=catalog.gaps[0]["reason"])
    if newer:
        plan = replace(plan, choices={}, evidence={}, line_plans=(), plan_gap="response_plan_selection_newer_than_revision")
    return replace(plan, source_selection=captured_selection_from_preferences(client.pk, preferences) if preferences else {},
        readiness_snapshot=readiness, payment_context_snapshot=payment_snapshot,
        payment_context_boundary=payment_boundary, payment_context_fence=payment_fence,
        payment_context_reason=payment_reason, payment_context_captured_at=payment_at)
