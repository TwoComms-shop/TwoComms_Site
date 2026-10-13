"""Current typed manager facts for the canonical live revision boundary.

Only the native execution reader supplies an offer. Raw operator DTOs, drafts,
model controls, paid receipt text and historical terms cannot create this frame.
"""
from copy import deepcopy
from types import SimpleNamespace
from dataclasses import dataclass, replace
import json
import re

from management.services.ig_assisted_commerce import VerifiedAssistedDecision

CLAIM = "assisted_commerce_offer"
ACTION = "assisted_checkout_proposal_create"
_ISSUER = object()


def should_capture_assisted_context(plan, client, revision=None):
    """No extra lookup on a ready ordinary catalogue path."""
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

    def __post_init__(self):
        if (self._issuer is not _ISSUER or not isinstance(self.verified, VerifiedAssistedDecision)
            or self.verified.proof_scope == "historical"):
            raise ValueError("assisted_context_requires_current_native_reader")

    @property
    def reference(self):
        value = self.verified
        return {"request_id": str(value.request_id), "decision_id": str(value.decision_id),
            "request_digest": value.request_digest, "decision_digest": value.decision_digest,
            "snapshot_digest": value.snapshot_digest, "intention_id": str(value.intention_id),
            "full_capture_digest": value.full_capture_digest, "revision_id": self.revision_id,
            "revision_digest": self.revision_digest, "cart_capture_digest": self.cart_capture_digest}

    @property
    def projection(self):
        return {"schema": "current-assisted-dialogue.v1", "reference": self.reference,
            "manager_checked_at": self.verified.physical_checked_at.isoformat() if self.verified.physical_checked_at else None,
            "expires_at": self.verified.expires_at.isoformat(), "currency": "UAH",
            "approved_total": str(self.verified.approved_total),
            "lines": [line.as_dict() for line in self.verified.lines]}

    def prompt_guidance(self):
        # Typed manager terms authorize this exact offer; they do not rewrite
        # customer choices, grant a catalogue ID or settle any payment.
        return ("[CURRENT VERIFIED MANAGER OFFER]\n" + json.dumps(self.projection, ensure_ascii=False, separators=(",", ":"))
            + "\nUse these exact manager-approved offer facts for the named current lines. READY is checked physical availability in the stated quantity. "
            "Manager terms differing from a source wish are a counteroffer; the hosted checkout must obtain customer adoption. "
            "Only confirmed answers to the exact original question are known; new fabric/hardware questions remain unanswered. "
            "A typed preparation/dispatch estimate is its own domain and never guarantees arrival. No typed estimate means timing unknown. "
            "Do not create product IDs, stock, discounts, paid money or order-created facts. Reference IDs are backend metadata, never customer prose.")

    def resolves_inquiry(self, inquiry):
        source = self.verified.source_proof
        questions = source.get("questions") or []
        answers = {answer["question_id"]: answer for line in self.verified.lines
                   for answer in line.as_dict()["question_answers"]}
        keys = ("source_message_id", "source_digest", "line_id", "axis", "requested_value", "question")
        return any(all(question.get(key) == inquiry.get(key) for key in keys)
            and answers.get(question.get("question_id"), {}).get("state") == "confirmed" for question in questions)

    def purchase_ready(self, revision, capture):
        from management.services.ig_response_cart_plan import source_line_preferences
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
                    line_id=line.line_id, line_label=line.title, source_ref=self.verified.decision_digest,
                    review_line_labels=tuple((value.line_id, value.title) for value in self.verified.lines)))
                if len(self.verified.lines) == 1:
                    times.append(replace(times[-1], line_id="", line_label=""))
        return replace(base, authorized_prices=(line.unit_price,) if line.unit_price is not None else (),
            authorized_price_ranges=(), allowed_sizes=(line.size,), allowed_fits=(line.fit_code,) if line.fit_code else (),
            allowed_colors=tuple(dict.fromkeys((line.color, _color(line.color)))),
            timing_authorities=tuple(times), approved_timing_claims=(), explicitly_qualified_standard_dispatch_days=None,
            availability_authorities=(), fulfillment_review_status="")

    def validate_reply(self, response, base):
        from management.services.ig_reply_truth import (_claim_sentences, _MONEY_RE, _product_availability_claim,
            validate_reply_truth, ReplyTruthResult, _TIMING_RE, _timing_nonclaim)
        lines = self.verified.lines
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
                    if (answer["state"] == "confirmed" and question.get("line_id") == line.line_id
                        and answer["answer"].strip() and exact_unit(framed) == exact_unit(answer["answer"])):
                        approved = True
                        break
            if not approved:
                honest = validate_inquiry_response({"status": "manager_required", "inquiries": [{"axis": "information"}]},
                    SimpleNamespace(control={"manager": True}, reply_text=part))
                if honest:
                    return ReplyTruthResult(False, ("product_information_unverified",))
        for clause in _claim_sentences(response.reply_text, ()):
            matched = [line for line in lines if re.search(re.escape(line.title), clause, re.I)
                or re.search(r"(?:поз\w*|позици\w*|item|position)\s*#?\s*" + str(line.original_index + 1) + r"(?!\d)", clause, re.I)]
            money = bool(_MONEY_RE.search(clause))
            stock = _product_availability_claim(clause)
            timing = any(not _timing_nonclaim(clause, match.start()) for match in _TIMING_RE.finditer(clause))
            if not (money or stock or timing):
                continue
            total = money and re.search(r"разом|итого|всього|total|весь\s+(?:кошик|заказ)", clause, re.I)
            if not matched and len(lines) == 1:
                matched = [lines[0]]
            if total and not stock and not timing:
                result = validate_reply_truth(clause, context=replace(base, authorized_prices=(self.verified.approved_total,),
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
        aggregate = replace(base, authorized_prices=tuple(dict.fromkeys([self.verified.approved_total, *[line.unit_price for line in lines if line.unit_price is not None]])),
            allowed_sizes=tuple(dict.fromkeys(line.size for line in lines)),
            allowed_colors=tuple(dict.fromkeys(color for context in contexts for color in context.allowed_colors)),
            allowed_fits=tuple(dict.fromkeys(line.fit_code for line in lines if line.fit_code)),
            timing_authorities=tuple(fact for context in contexts for fact in context.timing_authorities),
            authorized_price_ranges=(), approved_timing_claims=(), explicitly_qualified_standard_dispatch_days=None,
            availability_authorities=(), fulfillment_review_status="")
        return validate_reply_truth(response.reply_text, context=aggregate)

    def coverage(self, plan, response, revision, original):
        """Credit only current typed offer facts; payment/support debt remains."""
        from management.services.ig_reply_truth import ReplyTruthContext, _MONEY_RE
        if not self.validate_reply(response, ReplyTruthContext()).valid:
            return original
        result = deepcopy(original)
        additions = []
        for obligation in plan.obligations:
            if obligation["id"] not in result.get("remaining", ()):
                continue
            kind = obligation["kind"]
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
                                      result.get("can_prepare_checkout") is True, _ISSUER)
    if reference is not None and context.reference != reference:
        rebound = dict(reference) if isinstance(reference, dict) else {}
        if (isinstance(execution, VerifiedAssistedExecution) and execution.client_id == client.pk
            and rebound.get("cart_capture_digest") == before.get("capture_digest")):
            rebound["cart_capture_digest"] = context.cart_capture_digest
        if rebound != context.reference:
            return None, "assisted_offer_changed"
    return context, ""
