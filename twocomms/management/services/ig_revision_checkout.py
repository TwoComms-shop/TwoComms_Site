"""Atomic first-party checkout URL and complete revision effect preparation.

No provider calls, model prices, or inferred chat agreements enter this boundary.
The bearer URL is retained only in the exact durable delivery payload.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Mapping

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Max
from django.urls import reverse
from django.utils import timezone

from management.models import IgCheckoutAccessToken, IgClient, IgCustomerTurnRevision, InstagramBotSettings
from management.services.ig_checkout import CheckoutConfigurationError, create_or_update_proposal, validate_checkout_items
from management.services.ig_revision_actions import _authority_projection
from management.services.ig_revision_authority import (
    CLAIM_CATALOG_CONFIGURATION, CLAIM_CURRENT_OFFER, RevisionAuthorityBindingSet,
    build_revision_authority_bindings, check_fact_bindings, check_offer_bindings,
    capture_checkout_business_facts, owned_checkout_business_rebind,
)
from management.services.ig_revision_outbox import (
    PublicationBinding, _digest, plan_revision_effects, pre_winner_readiness,
)
from management.services.ig_revision_transport import prepare_text_effects

ACTION = "checkout_proposal_create"


def requested_nonstandard_carrier(client, sources, *, source_cart_capture=None):
    """An own current delivery choice needs its actual human arrangement."""
    from management.services.instagram_bot import _current_purchase_source_texts
    return _requested_carrier_for_rows(_current_purchase_source_texts(client, sealed_sources=sources,
        source_cart_capture=source_cart_capture, with_ids=True))


def _requested_carrier_for_rows(rows):
    import re
    from management.services.ig_commerce_turns import _current_source_text

    result = {}
    carriers = re.compile(r"\b(?P<ukr>укрпошт\w*|укрпочт\w*|ukrposhta)\b|"
        r"\b(?P<meest>meest|міст\s+експрес\w*|мист\s+экспресс\w*)\b|"
        r"\b(?P<pickup>самовив\w*|самовывоз\w*|pick\s*up)\b|"
        r"\b(?P<other>інш\w*\s+(?:служб\w*|перевізник\w*)|друг\w*\s+(?:служб\w*|перевозчик\w*)|another\s+carrier)\b|"
        r"\b(?P<np>нов(?:а|ою|ой)\s+пошт\w*|нов(?:ая|ой)\s+почт\w*|nova\s+poshta|нп)\b", re.I)
    for row in rows:
        for clause in re.split(r"[.!?;\n,]+|\b(?:але|а|но|but)\b", _current_source_text(row["text"]), flags=re.I):
            if re.search(r"\b(?:сказ\w*|казав\w*|говор\w*|said|told|якщо|если|if)\b", clause, re.I):
                continue
            if len(list(carriers.finditer(clause))) > 1 and re.search(r"\b(?:або|чи|или|or)\b", clause, re.I):
                result = {"carrier": "ambiguous", "source_message_id": row["message_id"], "source_quote": row["text"]}
                continue
            for match in carriers.finditer(clause):
                before, after = clause[:match.start()], clause[match.end():]
                negated = bool(re.search(r"\b(?:не|ні|not|no|без|without)\s*(?:(?:хочу|через|by|via)\s*){0,2}$", before, re.I)
                    or re.search(r"\b(?:не|not|don['’]t|do\s+not)\s+(?:відправ\w*|отправ\w*|send|ship|use)\s+(?:(?:через|by|via)\s+)?$", before, re.I)
                    or re.match(r"\s+не\s+(?:треба|потріб\w*|нуж\w*|відправ\w*|достав\w*|отправ\w*)\b", after, re.I))
                if negated:
                    if result.get("carrier") == match.lastgroup:
                        result = {}
                    continue
                addressed = bool(re.search(r"\b(?:відправ\w*|надішл\w*|надсила\w*|достав\w*|отрима\w*|отправ\w*|пришл\w*|получ\w*|"
                    r"хочу|потріб\w*|нуж\w*|можна|можно|можете|через|зараз|тепер|сейчас|теперь|now|send|ship|deliver|receiv\w*|prefer|via|by)\b", clause, re.I)
                    or not (before + after).strip(" :—-"))
                if not addressed:
                    continue
                result = {} if match.lastgroup == "np" else {"carrier": match.lastgroup,
                    "source_message_id": row["message_id"], "source_quote": row["text"]}
    return result


def requested_manual_delivery(client, sources, *, source_cart_capture=None):
    """Keep carrier and explicit distinct destinations as independent own wishes.

    The hosted form has one destination. No manager prose/case completion can
    turn a request for several destinations into authority for that form.
    """
    from management.services.instagram_bot import _current_purchase_source_texts

    rows = _current_purchase_source_texts(client, sealed_sources=sources,
        source_cart_capture=source_cart_capture, with_ids=True, include_window_state=True)
    carrier = _requested_carrier_for_rows(rows)
    destinations, single_source_id = _destination_requirement_for_rows(rows)
    if rows and rows[0].get("raw_window_saturated") and not destinations and not single_source_id:
        destinations = _retained_destination_requirement(client, oldest_message_id=rows[0]["message_id"],
            source_cart_capture=source_cart_capture)
    result = dict(carrier)
    if destinations:
        result["multiple_destinations"] = destinations
    return result


def _destination_requirement_for_rows(rows):
    import re
    from management.services.ig_commerce_turns import _current_source_text
    destinations = {}
    single_source_id = 0
    target = r"(?:адрес\w*|міст(?:о|а|ах|ам|ами)|город\w*|addresses|address|cities|destinations?)"
    multiple = re.compile(r"\b(?:дві|двох|двом\w*|двум\w*|два|две|2|різн\w*|разн\w*|окрем\w*|two|both|different|separate|multiple)\s+"
        r"(?:(?:різн\w*|разн\w*|different|separate)\s+)?" + target + r"\b", re.I)
    single = re.compile(r"\b(?:одн\w*|один|одну|one|single)\s+(?:" + target +
        r"|посилк\w*|посылк\w*|parcel|shipment)\b", re.I)
    shipping = r"\b(?:відправ\w*|надішл\w*|надсил\w*|достав\w*|отправ\w*|пришл\w*|send|ship\w*|deliver\w*)\b"
    negated_prefix = re.compile(r"\b(?:не|ні|not|no|без|without|don['’]t|do\s+not)\s*"
        r"(?:(?:хочу|хочемо|треба|потріб\w*|нуж\w*|надо|want|need|to|the|на|по|у|в|in|at|це|їх|усе|вс[её]|это|их|it|both|them|these|all|everything|"
        r"відправ\w*|надсил\w*|отправ\w*|достав\w*|send|ship\w*|deliver\w*)\s+){0,5}$", re.I)
    for row in rows:
        for sentence in re.split(r"[.!?;\n]+", _current_source_text(row["text"])):
            if re.search(r"\b(?:якщо|если|if|maybe|можливо|возможно)\b", sentence, re.I):
                continue
            if re.search(r"\b(?:сказ\w*|казав\w*|говор\w*|said|told)\b", sentence, re.I):
                present = re.search(r"\b(?:тепер|теперь|зараз|сейчас|now)\b", sentence, re.I)
                if not present:
                    continue
                sentence = sentence[present.start():]
            for clause in re.split(r",|\b(?:але|а|но|but|instead)\b", sentence, flags=re.I):
                if any(negated_prefix.search(clause[:action.start()]) for action in re.finditer(shipping, clause, re.I)):
                    continue
                multiple_in_clause = False
                addressed = bool(re.search(shipping + r"|\b(?:хочу|потріб\w*|нуж\w*|want|need|prefer)\b", clause, re.I))
                for match in re.finditer(multiple.pattern + "|" + single.pattern + r"|\b(?:усе\s+разом|все\s+разом|вс[её]\s+вместе|all\s+together|разом|вместе|together)\b", clause, re.I):
                    before, after = clause[:match.start()], clause[match.end():]
                    if negated_prefix.search(before) or re.match(r"\s+(?:(?:are|is)\s+)?(?:не\s+(?:треба|потріб\w*|нуж\w*)|not\s+(?:needed|required))\b", after, re.I):
                        continue
                    bare = bool(re.fullmatch(r"\s*(?:(?:тепер|теперь|зараз|сейчас|now|давайте|please)\s+)?"
                        r"(?:(?:на|по|у|в|to|at|in)\s+)?", before, re.I)
                        and (not after.strip(" :—-?") or re.match(r"\s*(?::|(?:до|у|в|to|in|at)\b)", after, re.I)))
                    explicit_target = bool(multiple.fullmatch(match.group()) or single.fullmatch(match.group()))
                    if not (bare or (addressed if explicit_target else re.search(shipping, clause, re.I))):
                        continue
                    if multiple.fullmatch(match.group()):
                        destinations = {"source_message_id": row["message_id"], "source_quote": row["text"]}
                        multiple_in_clause = True
                    else:
                        if multiple_in_clause:
                            continue  # One address per person enumerates the plural request.
                        destinations = {}
                        single_source_id = row["message_id"]
    return destinations, single_source_id


def _retained_destination_requirement(client, *, oldest_message_id, source_cart_capture=None):
    """A bounded native case retains only a negative delivery requirement.

    Unrelated chat cannot turn a multi-address request into single-form assent
    after the ordinary source window fills. Completed/raw manager decisions
    grant no exemption; a newer own single-address choice is handled above.
    """
    from django.db.models import Q
    from management.models import IgFollowUpTask, IgCustomerTurnRevision, IgCommercialEpisode, InstagramBotMessage, InstagramBotSettings
    from management.services.ig_conversation_routes import conversation_route_reset_floor
    from management.services.ig_purpose_sources import owned_source
    from management.services.ig_ingress_source_proof import is_current_customer_source
    from management.services.instagram_bot import ingress_provider_namespace
    scope = (source_cart_capture or {}).get("scope") or {}
    floor = scope.get("reset_floor") or conversation_route_reset_floor(client.pk)
    settings_row = InstagramBotSettings.objects.filter(pk=1).first()
    if settings_row is None:
        return {}
    episode = IgCommercialEpisode.objects.filter(pk=client.current_commercial_episode_id,
        client=client, state="active", open_slot=1).first()
    if episode is None:
        return {}
    namespace = ingress_provider_namespace(settings_row)
    tasks = list(IgFollowUpTask.objects.filter(client=client, kind="manager_task", reason="revision_case:manager_handoff",
        policy_version="revision-case-v1", manager_context__case_kind="customer_manager_request",
        manager_context__delivery_scope__episode_id=client.current_commercial_episode_id,
        manager_context__delivery_scope__reset_floor=floor, manager_context__delivery_scope__source_namespace=namespace,
        manager_context__delivery_request__multiple_destinations__source_message_id__lt=oldest_message_id,
        manager_context__delivery_request__multiple_destinations__source_message_id__gte=max(floor, episode.opened_watermark_message_id or 0),
    ).order_by("-pk")[:8])
    for task in tasks:
        context = task.manager_context or {}
        requirement = (context.get("delivery_request") or {}).get("multiple_destinations") or {}
        source = InstagramBotMessage.objects.filter(pk=requirement.get("source_message_id"), client=client).first()
        revision = IgCustomerTurnRevision.objects.filter(pk=context.get("latest_revision_id"), client=client).first()
        if source is None or revision is None or source.text != requirement.get("source_quote"):
            continue
        receipt = (revision.action_receipts or {}).get("manager_handoff") or {}
        if (receipt.get("task_id") != task.pk or receipt.get("case_kind") != "customer_manager_request"
            or receipt.get("generation_proposal_digest") != revision.generation_proposal_digest
            or not revision.generation_proposal_digest or _digest(revision.generation_proposal) != revision.generation_proposal_digest):
            continue
        owned, _reason = owned_source(revision, source, settings_row, require_head=False)
        if not (owned and owned[2]["namespace"] == namespace and owned[2]["reset_floor"] == floor):
            continue
        # A prior explicit combination may itself have left the recent window.
        # Screen a finite set of possible corrections, then reuse the same own
        # assertion parser. A saturated correction scan keeps the negative hold.
        candidates = Q(pk__in=[])
        for stem in ("одн", "разом", "вмест", "one", "single", "together"):
            candidates |= Q(text__icontains=stem)
        corrections = list(InstagramBotMessage.objects.filter(candidates, client=client,
            sender_id=client.igsid, role="user", pk__gt=source.pk, pk__lt=oldest_message_id,
            provider_namespace=namespace, source__in=("webhook", "poll")).exclude(status="failed").select_related("client").order_by("-pk")[:65])
        if len(corrections) > 64:
            return dict(requirement)
        current = [{"message_id": row.pk, "text": row.text} for row in reversed(corrections)
            if is_current_customer_source(row, namespace=namespace)]
        later, cleared = _destination_requirement_for_rows(current)
        if not later and cleared:
            continue
        return later or dict(requirement)
    return {}


def checkout_question_coverage(plan, response, sources, coverage):
    """Retain current question debt; an honest unconfirmed discount is an answer."""
    import re
    from management.services.ig_response_plan import _requested_topics, _unquoted_text

    if not (response.control.get("paylink") or response.control.get("payment")):
        return coverage
    result = deepcopy(coverage)
    by_id = {row.get("message_id"): row for row in sources if isinstance(row, dict) and row.get("role") == "user"}
    payment_ids = {row["source_message_id"] for row in plan.obligations
        if row["id"] in result.get("remaining", ()) and row["kind"].startswith("info:payment")}
    if payment_ids:
        client_id = plan.scope.get("client_id") or (plan.source_cart_capture.get("scope") or {}).get("client_id")
        if type(client_id) is int:
            policy, reasons = _prepay_policy(IgClient(pk=client_id), sources)
            if not reasons:
                # The authenticated current 200+COD choice is answered by the
                # existing hosted payment policy, not an invented prose amount.
                for row in plan.obligations:
                    if row["source_message_id"] == policy.evidence_message_id and row["kind"].startswith("info:payment") and row["id"] in result["remaining"]:
                        result["remaining"].remove(row["id"])
                        result.setdefault("covered", []).append(row["id"])
    discount = r"\b(?:зниж\w*|скид\w*|discount\w*)\b"
    honest_discount = any(re.search(discount, clause, re.I) and re.search(
        r"не\s+(?:підтвердж\w*|подтвержд\w*)|не\s+маю\s+підтвердж\w*|"
        r"немає\s+підтверджен\w*|(?:не\s+можу|не\s+могу)\s+підтверд\w*|(?:cannot|can['’]t)\s+confirm|unconfirmed|not\s+confirmed", clause, re.I)
        for clause in re.split(r"[.!?\n]+", _unquoted_text(response.reply_text)))
    pending_discount = any(re.search(discount, clause, re.I) and re.search(
        r"\b(?:уточн\w*|перевір\w*|погод\w*|уточн\w*|провер\w*)\b|\b(?:will|shall)\b.*\b(?:check|ask|confirm)\b", clause, re.I)
        for clause in re.split(r"[.!?\n]+", _unquoted_text(response.reply_text)))
    if honest_discount and not pending_discount:
        for obligation in plan.obligations:
            if obligation["kind"] != "info:question" or obligation["id"] not in result.get("remaining", ()):
                continue
            text = _unquoted_text((by_id.get(obligation["source_message_id"]) or {}).get("text") or "")
            unknown = [clause for clause in re.findall(r"[^.!?\n]+[?]?", text)
                if "info:question" in _requested_topics(clause)]
            if (unknown and all(re.search(discount, clause, re.I) for clause in unknown)
                and re.search(r"парн\w*|для\s+двох|для\s+двоих|pair\w*|couple\w*|\btwo\b", text, re.I)):
                result["remaining"].remove(obligation["id"])
                result.setdefault("covered", []).append(obligation["id"])
    if not result.get("remaining"):
        result["disposition"] = "complete"
    return result


def checkout_question_reason(plan, response, sources, *, coverage=None, checkout_cart_binding=None):
    """Configuration and binding own purchase debt; questions must be answered first."""
    if plan.plan_gap:
        return plan.plan_gap
    coverage = checkout_question_coverage(plan, response, sources,
        coverage if coverage is not None else plan.coverage(response, checkout_cart_binding=checkout_cart_binding))
    remaining = set(coverage.get("remaining", ()))
    from management.services.ig_commerce_turns import parse_turn
    ambiguous = {row.get("message_id") for row in sources if isinstance(row, dict) and row.get("role") == "user"
        and (parse_turn(row.get("text") or "").pending_line_clarification or parse_turn(row.get("text") or "").pending_clarification)}
    if any(row["id"] in remaining and (row["kind"].startswith(("info:", "payment:"))
            or (row["kind"] == "unresolved_line_operation" and row["source_message_id"] in ambiguous)) for row in plan.obligations):
        return "response_plan_checkout_questions_unanswered"
    return ""


@dataclass(frozen=True)
class RevisionCheckoutResult:
    planned: bool = False
    created: bool = False
    effects: tuple = ()
    proposal_id: int = 0
    reasons: tuple[str, ...] = ()


class _Rollback(Exception):
    pass


def _payment_request(control):
    """Provider controls select a candidate; they cannot authorize an amount."""
    from management.services.ig_checkout_policy import PREPAY_200_AMOUNT

    paylink = str(control.get("paylink") or "").casefold()
    payment = control.get("payment")
    if paylink not in {"", "full", "online_full", "true", "prepay"}:
        return "", ("checkout_payment_policy_unsupported",)
    # As in finalize_paylink, a full-payment control ignores a model amount.
    # The amount remains the independently validated catalog total.
    if paylink in {"full", "online_full", "true"}:
        return "full", ()
    if payment is not None:
        try:
            amount = Decimal(str(payment).replace(",", "."))
        except (InvalidOperation, ValueError):
            return "", ("checkout_payment_amount_unsupported",)
        if not amount.is_finite() or amount != Decimal(PREPAY_200_AMOUNT):
            return "", ("checkout_payment_amount_unsupported",)
    return ("prepay" if paylink == "prepay" or payment is not None else "full"), ()


def _prepay_policy(client, sealed_sources):
    """Reproduce the existing 200+COD gate from exact sealed customer evidence."""
    from management.models import IgCheckoutProposal, InstagramBotMessage
    from management.services.ig_checkout_policy import resolve_payment_policy
    from management.services.ig_turn_revisions import MAX_SOURCES

    if not isinstance(sealed_sources, (list, tuple)) or not 1 <= len(sealed_sources) <= MAX_SOURCES:
        return None, ("checkout_payment_source_missing",)
    sources = {
        row.get("message_id"): row for row in sealed_sources
        if isinstance(row, Mapping) and row.get("role") == "user"
    }
    decision = resolve_payment_policy(client=client, evidence_message_ids=tuple(sources))
    if decision.policy != IgCheckoutProposal.PaymentPolicy.FULL_OR_200_COD:
        return None, ("checkout_prepay_not_authorized",)
    snapshot = sources.get(decision.evidence_message_id)
    source = InstagramBotMessage.objects.filter(
        pk=decision.evidence_message_id, client=client,
        role=InstagramBotMessage.Role.USER,
    ).only("text", "quick_reply_payload").first()
    if source is None or snapshot is None or (
        str(snapshot.get("text") or "") != str(source.text or "")
        or str(snapshot.get("quick_reply_payload") or "") != str(source.quick_reply_payload or "")
    ):
        return None, ("checkout_payment_source_changed",)
    return decision, ()


def checkout_owner_scope(client):
    """Exact mutable business owner; source DTO hashes are not this permission."""
    from management.models import IgCommercialEpisode, IgDeal

    current = IgClient.objects.filter(pk=client.pk).values(
        "pk", "igsid", "current_commercial_episode_id", "reply_permission_epoch",
        "privacy_erasure_started_at", "bot_paused", "manager_takeover", "is_blocked",
        "hidden_at", "opted_out_at",
    ).first()
    if current is None or current["privacy_erasure_started_at"]:
        return None
    episode = IgCommercialEpisode.objects.filter(pk=current["current_commercial_episode_id"],
        client_id=client.pk).values("pk", "client_id", "state", "open_slot", "intended_order_id",
            "deal_id", "primary_payment_review_id", "updated_at").first()
    if episode is None or episode["state"] != IgCommercialEpisode.State.ACTIVE or episode["open_slot"] != 1 or episode["intended_order_id"] is not None:
        return None
    deal = None
    if episode["deal_id"] is not None:
        deal = IgDeal.objects.filter(pk=episode["deal_id"], client_id=client.pk).values(
            "pk", "client_id", "status", "order_id", "paid_at", "paid_amount", "payment_truth",
            "active_checkout_proposal_id", "invoice_id", "invoice_url", "updated_at").first()
        if (deal is None or deal["order_id"] is not None or deal["paid_at"] or (deal["paid_amount"] or 0) > 0
                or deal["invoice_id"] or deal["invoice_url"]
                or deal["status"] in {IgDeal.Status.PAID, IgDeal.Status.ORDER_CREATED}
                or deal["payment_truth"] in {IgDeal.PaymentTruth.CONFIRMED, IgDeal.PaymentTruth.PARTIALLY_REFUNDED,
                    IgDeal.PaymentTruth.REFUNDED, IgDeal.PaymentTruth.REVERSED}):
            return None
    # The existing authority serializer also uses ISO timestamps and decimals.
    from management.services.ig_revision_authority import _jsonable
    return _jsonable({"client": current, "episode": episode, "deal": deal})


def rebind_checkout_cart_owner(control, *, before, after, checkout):
    """Allow only this transaction's exact offer attachment, no cart mutation."""
    if not isinstance(before, dict) or not isinstance(after, dict) or before.get("client") != after.get("client"):
        return None
    old_episode, new_episode = before.get("episode"), after.get("episode")
    if not isinstance(old_episode, dict) or not isinstance(new_episode, dict):
        return None
    if any(old_episode.get(key) != new_episode.get(key) for key in old_episode if key not in {"deal_id", "updated_at"}):
        return None
    if new_episode.get("pk") != checkout.commercial_episode_id or new_episode.get("deal_id") != checkout.deal_id or old_episode.get("deal_id") not in {None, checkout.deal_id}:
        return None
    old_deal, new_deal = before.get("deal"), after.get("deal")
    if not isinstance(new_deal, dict) or new_deal.get("pk") != checkout.deal_id or new_deal.get("active_checkout_proposal_id") != checkout.pk:
        return None
    if old_deal is not None and (not isinstance(old_deal, dict) or any(
        old_deal.get(key) != new_deal.get(key) for key in old_deal if key not in {"active_checkout_proposal_id", "status", "updated_at"}
    )):
        return None
    from management.models import IgDeal
    if (new_deal.get("status") != IgDeal.Status.QUOTED and before != after) or (old_deal is not None
            and old_deal.get("status") not in {IgDeal.Status.DRAFT, IgDeal.Status.QUOTED, IgDeal.Status.AWAITING_PAYMENT}):
        return None
    if new_deal.get("order_id") is not None or new_deal.get("paid_at") or Decimal(new_deal.get("paid_amount") or "0") > 0 or new_deal.get("invoice_id") or new_deal.get("invoice_url"):
        return None
    if control.get("checkout_owner_scope") != before:
        return None
    result = deepcopy(control)
    result["checkout_owner_scope"] = deepcopy(after)
    return result


def _source_cart_control(client, control, captured):
    from management.services.ig_commerce_projection import capture_current_selection_lines
    from management.services.ig_checkout_readiness import selection_readiness
    from management.services.ig_commerce_turns import parse_turn
    from management.services.ig_revision_cart_binding import (
        build_cart_binding, bind_quote_lines, frozen_cart_binding_payload,
        same_checkout_source_capture, stable_checkout_source_capture,
    )

    if stable_checkout_source_capture(captured) is None:
        return {}, ("checkout_cart_capture_invalid",)
    current = capture_current_selection_lines(client.pk)
    if not same_checkout_source_capture(captured, current):
        return {}, ("checkout_cart_source_changed",)
    from management.services.ig_fulfillment_checkout import fulfillment_deadline_checkout_reason
    deadline_reason = fulfillment_deadline_checkout_reason(client, current)
    if deadline_reason:
        return {}, (deadline_reason,)
    from management.models import IgCommerceSelectionSession
    from management.services.ig_commerce_projection import _choice_digest
    session = IgCommerceSelectionSession.objects.filter(pk=current["session_id"], client_id=client.pk,
        commercial_episode_id=current["scope"]["episode_id"], generation=current["generation"],
        revision=current["selection_revision"], open_slot=1, state="open").first()
    if session is None or _choice_digest(session.snapshot()) != current["fence"]["snapshot_digest"]:
        return {}, ("checkout_cart_source_changed",)
    if session.pending_clarification:
        return {}, ("checkout_cart_clarification_required",)
    owner = checkout_owner_scope(client)
    if owner is None or owner["episode"]["pk"] != captured["scope"].get("episode_id"):
        return {}, ("checkout_cart_owner_unavailable",)
    if "checkout_owner_scope" in control and control["checkout_owner_scope"] != owner:
        return {}, ("checkout_cart_owner_changed",)
    source = build_cart_binding(captured_cart=captured, expected_scope=current["scope"])
    if not source.ok:
        return {}, (source.reason or "checkout_cart_incomplete",)
    specs, readiness = [], {}
    for line in source.binding["lines"]:
        choices = line["choices"]
        quantity = choices.get("quantity", (line["quantity_default"] or {}).get("value"))
        args = {"product_id": choices.get("product_id"), "size": choices.get("size", ""),
            "fit": choices.get("fit_option_code", ""), "quantity": quantity,
            "color": choices.get("color", ""), "strict": True}
        state = selection_readiness(selection={}, **args)
        color = choices.get("color")
        if color:
            # Existing parser aliases classify catalog names too. A source color
            # can identify one current variant; ambiguous shades still abstain.
            matches = [row["variant_id"] for row in state["color"]["options"] if (
                str(row["name"]).strip().casefold() == str(color).strip().casefold()
                or parse_turn(row["name"]).field_updates.get("color") == color)]
            if len(matches) != 1:
                return {}, ("checkout_cart_color_unresolved",)
            state = selection_readiness(selection={"color_variant_id": matches[0]}, **args)
        if state.get("applicability_known") is not True or state.get("can_issue_link") is not True or state.get("missing"):
            return {}, ("checkout_cart_readiness_incomplete",)
        item = {"product_id": state["product"]["id"], "qty": state["quantity"],
            "size": state["size"]["selected"], "fit_option_code": state["fit"]["selected"],
            "color_variant_id": state["color"]["selected_variant_id"], "option_values": state["options"]["selected"]}
        specs.append(item)
        readiness[line["line_id"]] = {"scope": {**captured["scope"], "line_id": line["line_id"], "recipient_id": line["recipient_id"]},
            "capture_digest": captured["capture_digest"], "readiness": state}
    try:
        quote = validate_checkout_items(client=client, item_specs=specs, negotiated_total=None,
            source_cart_binding=source)
    except CheckoutConfigurationError as exc:
        return {}, ("checkout:" + exc.code,)
    items = _quote_items(quote)
    result = bind_quote_lines(binding=source, quoted_items=items, readiness_by_line=readiness)
    if result.status != "ready":
        return {}, (result.reason or "checkout_cart_binding_incomplete",)
    binding = frozen_cart_binding_payload(result)
    if "source_cart_binding" in control and control["source_cart_binding"] != binding:
        return {}, ("checkout_cart_binding_changed",)
    return {"items": items, "source_cart_binding": binding,
        "source_cart_capture": deepcopy(captured), "checkout_owner_scope": owner}, ()


def _quote_items(quote):
    return [{"product_id": item.product.pk,
        "color_variant_id": item.color_variant.pk if item.color_variant else None,
        "qty": item.quantity, "size": item.size,
        "fit_option_code": item.fit_code, "option_values": dict(item.option_values)} for item in quote.items]


def compact_checkout_control(control, *, artifact=None):
    """Fixed-size authority reference; full source artifacts stay with owners."""
    from management.services.ig_revision_cart_binding import stable_checkout_source_capture
    capture, binding = control["source_cart_capture"], control["source_cart_binding"]
    stable = stable_checkout_source_capture(capture)
    if stable is None:
        raise ValueError("checkout_cart_capture_invalid")
    artifact = artifact or control.get("source_cart_artifact")
    if not isinstance(artifact, dict):
        raise ValueError("checkout_generation_artifact_missing")
    return {"source_cart_reference": {
        "schema": "ig-checkout-cart-reference.v1",
        "semantic_digest": _digest(stable),
        "original_capture_digest": capture["capture_digest"],
        "source_binding_digest": binding["source_binding_digest"],
        "quote_binding_digest": binding["quote_binding_digest"],
        "quote_map_digest": _digest(binding["quote_line_map"]),
        "configuration_digest": _digest(control["items"]),
        "owner_scope_digest": _digest(control["checkout_owner_scope"]),
        "artifact": deepcopy(artifact),
    }}


def checkout_artifact_control(client, reference):
    """Load an original artifact from its exact current owned revision only."""
    from management.models import IgCheckoutRevision
    artifact = reference.get("artifact") if isinstance(reference, Mapping) else None
    if not isinstance(artifact, dict):
        return {}, ("checkout_cart_reference_invalid",)
    if artifact.get("kind") == "generation_capture":
        if set(artifact) != {"kind", "revision_id"} or type(artifact.get("revision_id")) is not int or artifact["revision_id"] <= 0:
            return {}, ("checkout_cart_reference_invalid",)
        revision = IgCustomerTurnRevision.objects.filter(pk=artifact["revision_id"], client_id=client.pk).first()
        proposal = revision.generation_proposal if revision is not None else None
        if revision is None or revision.sealed_at is None:
            return {}, ("checkout_generation_artifact_missing",)
        if not revision.generation_proposal_digest:
            now = timezone.now()
            if (proposal or revision.generation_proposed_at or revision.state != revision.State.CLAIMED
                    or not revision.claim_token or revision.lease_until is None or revision.lease_until <= now
                    or revision.overall_deadline <= now or revision.permission_epoch != client.reply_permission_epoch
                    or revision.erasure_started_at_snapshot is not None):
                return {}, ("checkout_generation_owner_unavailable",)
            # Before the first immutable generation commit only: no owner
            # materialization has happened, so ALL original hashes must match
            # the independently rebuilt canonical capture in the caller below.
            from management.services.ig_commerce_projection import capture_current_selection_lines
            return _source_cart_control(client, {}, capture_current_selection_lines(client.pk))
        if not isinstance(proposal, dict) or _digest(proposal) != revision.generation_proposal_digest:
            return {}, ("checkout_generation_artifact_missing",)
        facts = (proposal.get("authority") or {}).get("fact_bindings") or []
        if not any(row.get("claim") == CLAIM_CATALOG_CONFIGURATION and
                (row.get("selector") or {}).get("source_cart_reference") == reference for row in facts if isinstance(row, dict)):
            return {}, ("checkout_generation_artifact_scope_changed",)
        capture = proposal.get("source_cart_capture")
        return _source_cart_control(client, {}, capture)
    keys = {"kind", "proposal_id", "checkout_revision_id", "revision"}
    if set(artifact) != keys or artifact.get("kind") != "checkout_revision" or any(
            type(artifact.get(key)) is not int or artifact[key] <= 0 for key in keys - {"kind"}):
        return {}, ("checkout_cart_reference_invalid",)
    owner = checkout_owner_scope(client)
    if owner is None or not owner.get("deal") or owner["deal"]["active_checkout_proposal_id"] != artifact["proposal_id"]:
        return {}, ("checkout_cart_artifact_owner_changed",)
    revision = IgCheckoutRevision.objects.filter(pk=artifact["checkout_revision_id"],
        proposal_id=artifact["proposal_id"], revision=artifact["revision"],
        proposal__revision=artifact["revision"], proposal__client_id=client.pk,
        proposal__commercial_episode_id=owner["episode"]["pk"], proposal__deal_id=owner["deal"]["pk"]).first()
    snapshot = revision.snapshot if revision is not None else None
    if not isinstance(snapshot, dict) or "source_cart_capture" not in snapshot or "source_cart_binding" not in snapshot:
        return {}, ("checkout_cart_artifact_missing",)
    return _source_cart_control(client, {"source_cart_binding": snapshot["source_cart_binding"]},
        snapshot["source_cart_capture"])


def resolve_checkout_cart_reference(client, reference, *, source_cart_capture=None):
    """Fresh complete cart/readiness must reproduce every compact fence."""
    if not isinstance(reference, dict) or reference.get("schema") != "ig-checkout-cart-reference.v1":
        return {}, ("checkout_cart_reference_invalid",)
    if source_cart_capture is not None:
        normalized, reasons = _source_cart_control(client, {}, source_cart_capture)
    else:
        normalized, reasons = checkout_artifact_control(client, reference)
    if reasons:
        return {}, reasons
    try:
        current = compact_checkout_control(normalized, artifact=reference.get("artifact"))
    except (KeyError, TypeError, ValueError):
        return {}, ("checkout_cart_reference_invalid",)
    if current["source_cart_reference"] != reference:
        return {}, ("checkout_cart_reference_changed",)
    return normalized, ()


def lock_checkout_cart_sources(client, captured):
    """Use the existing source rows, after the client lock, through effect CAS."""
    from management.models import InstagramBotMessage
    ids = (captured.get("fence") or {}).get("source_ids") if isinstance(captured, dict) else None
    if (not isinstance(ids, list) or not 1 <= len(ids) <= 64
            or any(type(value) is not int or value <= 0 for value in ids)
            or ids != sorted(set(ids))):
        return False
    rows = list(InstagramBotMessage.objects.select_for_update().filter(pk__in=ids,
        client_id=client.pk).order_by("pk").values_list("pk", flat=True))
    return rows == ids


def checkout_authority_control(client, control, *, source_cart_capture=None, source_cart_revision_id=None):
    """Resolve provider selectors to a complete server-owned standard cart."""
    from management.services import instagram_bot as bot

    if not isinstance(control, Mapping) or control.get("_invalid"):
        return {}, ("checkout_control_invalid",)
    if "custom" in str(client.intent or "").casefold() or control.get("custom_print_lead_id"):
        return {}, ("custom_confirmation_and_price_authority_missing",)
    _request, reasons = _payment_request(control)
    if reasons:
        return {}, reasons
    captured = source_cart_capture if source_cart_capture is not None else control.get("source_cart_capture")
    if "source_cart_reference" in control:
        try:
            return resolve_checkout_cart_reference(client, control["source_cart_reference"], source_cart_capture=captured)
        except (TypeError, ValueError, KeyError, AttributeError):
            return {}, ("checkout_cart_readiness_unavailable",)
    if captured is not None:
        try:
            normalized, reasons = _source_cart_control(client, control, captured)
            if not reasons:
                artifact = control.get("source_cart_artifact")
                if source_cart_revision_id is not None:
                    if type(source_cart_revision_id) is not int or source_cart_revision_id <= 0:
                        return {}, ("checkout_cart_reference_invalid",)
                    artifact = {"kind": "generation_capture", "revision_id": source_cart_revision_id}
                if artifact is not None:
                    normalized["source_cart_artifact"] = deepcopy(artifact)
            return normalized, reasons
        except (TypeError, ValueError, KeyError, AttributeError):
            return {}, ("checkout_cart_readiness_unavailable",)
    if "source_cart_binding" in control or "checkout_owner_scope" in control:
        return {}, ("checkout_cart_capture_missing",)
    if "items" in control:
        specs = bot._control_item_specs(dict(control))
        if not specs:
            return {}, ("checkout_items_invalid",)
    else:
        product_id = bot._control_product_id(dict(control)) or client.current_product_id
        selection = bot._checkout_selection_state(client, product_id)
        options = bot._control_option_values(dict(control))
        if options is None:
            return {}, ("checkout_options_invalid",)
        specs = [{
            "product_id": product_id,
            "qty": control.get("qty", client.current_qty or 1),
            "size": control.get("size", client.current_size or ""),
            "fit_option_code": control.get("fit", selection.get("fit_option_code", "")),
            "color_variant_id": control.get("variant") or control.get("color_variant_id") or selection.get("color_variant_id"),
            "option_values": {**(selection.get("option_values") or {}), **options},
        }]
    from management.models import IgCommerceSelectionSession
    session = IgCommerceSelectionSession.objects.filter(client_id=client.pk, open_slot=1, state="open").first()
    if len(specs) != 1 or (session is not None and len(session.lines or []) > 1):
        return {}, ("checkout_cart_capture_missing",)
    try:
        quote = validate_checkout_items(client=client, item_specs=specs, negotiated_total=None)
    except CheckoutConfigurationError as exc:
        return {}, ("checkout:" + exc.code,)
    # The legacy single-item entrance still uses native current sources for a
    # cutoff hold. Model item dictionaries and client cache cannot clear it.
    from management.services.ig_commerce_projection import capture_current_selection_lines
    from management.services.ig_fulfillment_checkout import fulfillment_deadline_checkout_reason
    deadline_reason = fulfillment_deadline_checkout_reason(client, capture_current_selection_lines(client.pk))
    if deadline_reason:
        return {}, (deadline_reason,)
    return {"items": _quote_items(quote)}, ()


def authorize_revision_checkout(client, control, source_text, *, sealed_sources=(), source_cart_capture=None, source_cart_revision_id=None, assisted_context=None):
    """Use the real purchase gate with sealed customer text, never model prose."""
    from management.services.instagram_bot import payment_link_allowed
    if requested_manual_delivery(client, sealed_sources, source_cart_capture=source_cart_capture):
        return {}, ("response_plan_checkout_delivery_requires_manager",)
    if assisted_context is not None:
        from management.services.ig_assisted_commerce_context import AssistedCommerceContext
        from management.models import IgCustomerTurnRevision
        if not isinstance(assisted_context, AssistedCommerceContext) or assisted_context.verified.client_id != client.pk:
            return {}, ("assisted_checkout_context_invalid",)
        revision = IgCustomerTurnRevision.objects.filter(pk=source_cart_revision_id, client=client).first()
        if (not revision or revision.snapshot_digest != assisted_context.revision_digest
            or not assisted_context.purchase_ready(revision, source_cart_capture or {})):
            return {}, ("checkout_purchase_authority_missing",)
        if any(key in control for key in ("items", "item", "product", "color_variant_id", "option", "price", "price_quoted")):
            return {}, ("assisted_checkout_terms_override_forbidden",)
        request, reasons = _payment_request(control)
        if reasons or request != "full":
            return {}, reasons or ("assisted_checkout_payment_policy_unavailable",)
        return {"assisted_context": assisted_context, "assisted_reference": assisted_context.reference}, ()

    normalized, reasons = checkout_authority_control(client, control, source_cart_capture=source_cart_capture,
        source_cart_revision_id=source_cart_revision_id)
    if reasons:
        return normalized, reasons
    request, _reasons = _payment_request(control)
    if request == "prepay":
        _decision, reasons = _prepay_policy(client, sealed_sources)
        if reasons:
            return {}, reasons
    # The legacy purchase gate parses provider ITEM strings, not backend spec
    # dicts. It checks purchase intent using an already proven first product;
    # complete configuration authority remains the all-line binding above.
    purchase_control = ({"product": normalized["items"][0]["product_id"],
        **{key: control[key] for key in ("paylink", "payment") if key in control}}
        if "source_cart_binding" in normalized else dict(control))
    if not payment_link_allowed(client, purchase_control, str(source_text or ""),
            sealed_sources=sealed_sources, source_cart_capture=source_cart_capture):
        return {}, ("checkout_purchase_authority_missing",)
    return normalized, ()


def _response(proposal):
    from management.services.ig_response_control import ResponseControl, ValidatedResponse

    row = proposal.get("response") or {}
    return ValidatedResponse(
        reply_text=row.get("reply_text") or "",
        controls=tuple(ResponseControl(item["kind"], item["value"]) for item in row.get("controls") or ()),
    )


def _composition(revision, response, reply_text):
    """Only the saved model text and existing deterministic catalog hint qualify."""
    from management.services import instagram_bot as bot

    expected = response.reply_text
    selection = bot._catalog_media_selection_for_control(response.control, revision.client)
    if selection is not None:
        expected = bot._append_more_products_hint(expected, selection, revision.client)
    if reply_text != expected or not reply_text or len(reply_text) > 4500:
        raise _Rollback("checkout_text_contract_mismatch")


def prepare_revision_checkout(
    revision_id: int, revision_token: str, *, source_message_id: int,
    settings_id: int, settings_permission_epoch: int, publication: PublicationBinding,
    generation_proposal_digest: str, authority: RevisionAuthorityBindingSet,
    noncheckout_effects=(), reply_text: str, source_cart_capture=None, now=None,
) -> RevisionCheckoutResult:
    """Commit offer, one token, and ALL physical parts or roll everything back."""
    if connection.in_atomic_block:
        return RevisionCheckoutResult(reasons=("caller_transaction_active",))
    if not isinstance(authority, RevisionAuthorityBindingSet) or not authority.ready or ACTION not in authority.allowed_actions:
        return RevisionCheckoutResult(reasons=("checkout_action_not_authorized",))
    identity = IgCustomerTurnRevision.objects.filter(pk=revision_id).values("client_id").first()
    if identity is None:
        return RevisionCheckoutResult(reasons=("revision_missing",))
    now = now or timezone.now()
    try:
        with transaction.atomic():
            settings_obj = InstagramBotSettings.objects.select_for_update().select_related("active_instruction_publication").filter(pk=settings_id).first()
            client = IgClient.objects.select_for_update().filter(pk=identity["client_id"]).first()
            revision = IgCustomerTurnRevision.objects.select_for_update().filter(pk=revision_id, client_id=identity["client_id"]).first()
            if revision is None or client is None or settings_obj is None:
                raise _Rollback("checkout_identity_missing")
            proposal = revision.generation_proposal
            if not generation_proposal_digest or revision.generation_proposal_digest != generation_proposal_digest or not isinstance(proposal, Mapping) or _digest(proposal) != generation_proposal_digest:
                raise _Rollback("generation_proposal_mismatch")
            captured_publication = (proposal.get("policy_manifest") or {}).get("instruction_publication") or {}
            if any(captured_publication.get(key) != value for key, value in (
                ("id", publication.publication_id), ("version", publication.version),
                ("hash", publication.snapshot_hash),
            )):
                raise _Rollback("generation_publication_mismatch")
            before = proposal.get("authority") or {}
            supplied = _authority_projection(authority)
            receipt = (revision.action_receipts or {}).get("client_configuration_update") or {}
            if supplied != before and not (
                receipt.get("before_authority") == before
                and receipt.get("after_authority") == supplied
                and receipt.get("generation_proposal_digest") == generation_proposal_digest
                and receipt.get("source_message_id") == source_message_id
            ):
                raise _Rollback("generation_proposal_authority_mismatch")
            if ACTION not in before.get("allowed_actions", ()):
                raise _Rollback("stored_checkout_action_not_authorized")
            cart_facts = [row for row in authority.fact_bindings if row.get("claim") == CLAIM_CATALOG_CONFIGURATION]
            cart_reference = (cart_facts[0].get("selector") or {}).get("source_cart_reference") if len(cart_facts) == 1 else None
            if cart_reference is not None and cart_reference.get("artifact") != {"kind": "generation_capture", "revision_id": revision.pk}:
                raise _Rollback("checkout_cart_generation_reference_mismatch")
            cart_capture = source_cart_capture if source_cart_capture is not None else proposal.get("source_cart_capture")
            if cart_reference is not None and cart_capture is None:
                recovered, reasons = checkout_artifact_control(client, cart_reference)
                if reasons:
                    raise _Rollback(",".join(reasons))
                cart_capture = recovered["source_cart_capture"]
            if cart_reference is not None and (not isinstance(cart_capture, dict)
                    or cart_capture.get("capture_digest") != cart_reference.get("original_capture_digest")):
                raise _Rollback("checkout_cart_generation_capture_mismatch")
            if cart_capture is not None and not lock_checkout_cart_sources(client, cart_capture):
                raise _Rollback("checkout_cart_source_scope_invalid")
            source = revision.sources.filter(message_id=source_message_id, message__client_id=client.pk).first()
            snapshots = {row.get("message_id"): row for row in (revision.bundle_snapshot or {}).get("sources", ())}
            if source is None or source_message_id not in snapshots or source_message_id not in {row.get("message_id") for row in proposal.get("sources", ())}:
                raise _Rollback("source_not_in_revision")
            if snapshots[source_message_id].get("role") != "user":
                raise _Rollback("checkout_customer_source_required")
            generation = proposal.get("generation") or {}
            request_id = str(generation.get("request_id") or "")
            if not request_id:
                raise _Rollback("generation_request_missing")
            response = _response(proposal)
            _composition(revision, response, reply_text)
            prior = tuple(noncheckout_effects)
            if any(not isinstance(row, Mapping) or row.get("group") != "catalog_media" or row.get("kind") != "image" for row in prior):
                raise _Rollback("checkout_noncheckout_effect_invalid")
            plan_identity = _digest({
                "boundary": ACTION, "generation_proposal_digest": generation_proposal_digest,
                "source_message_id": source_message_id, "authority": supplied,
                "noncheckout_effects": prior, "reply_text": reply_text,
                "payment_policy": "hosted_catalog_standard_promo",
            })
            # The whole plan is the crash receipt. Never issue a replacement token
            # when an earlier preparation already committed its exact payload.
            existing = tuple(revision.delivery_effects.order_by("order_index", "id"))
            if existing:
                if any(row.authority_context_digest != plan_identity or row.generation_request_id != request_id or row.source_message_id != source_message_id or row.plan_digest != existing[0].plan_digest for row in existing):
                    raise _Rollback("checkout_plan_conflict")
                first = existing[0]
                if (first.settings_id_snapshot != settings_id
                    or first.settings_permission_epoch != settings_permission_epoch
                    or first.publication_id != publication.publication_id
                    or first.publication_version != publication.version
                    or first.publication_hash != publication.snapshot_hash):
                    raise _Rollback("checkout_plan_publication_mismatch")
                readiness = pre_winner_readiness(
                    revision.pk, revision_token, settings_id=settings_id,
                    settings_permission_epoch=settings_permission_epoch, publication=publication,
                    fact_bindings=first.fact_bindings, offer_bindings=first.offer_bindings,
                    fact_checker=check_fact_bindings, offer_checker=check_offer_bindings, now=now,
                )
                if not readiness.ready:
                    raise _Rollback("readiness:" + ",".join(readiness.reasons))
                offers = [row for row in first.offer_bindings if row.get("claim") == CLAIM_CURRENT_OFFER]
                if len(offers) != 1 or not offers[0].get("selector", {}).get("checkout_access_token_id"):
                    raise _Rollback("checkout_plan_offer_missing")
                return RevisionCheckoutResult(True, False, existing, offers[0]["subjects"]["proposal_id"])
            readiness = pre_winner_readiness(
                revision.pk, revision_token, settings_id=settings_id,
                settings_permission_epoch=settings_permission_epoch, publication=publication,
                fact_bindings=authority.fact_bindings, offer_bindings=authority.offer_bindings,
                fact_checker=check_fact_bindings, offer_checker=check_offer_bindings, now=now,
            )
            if not readiness.ready:
                raise _Rollback("readiness:" + ",".join(readiness.reasons))
            sealed_sources = tuple(snapshots.values())
            bound = [row for row in authority.fact_bindings if row.get("claim") == CLAIM_CATALOG_CONFIGURATION]
            if len(bound) != 1:
                raise _Rollback("checkout_cart_authority_mismatch")
            cart_selector = bound[0].get("selector") or {}
            original_capture = cart_capture if cart_reference is not None else None
            checkout_control = dict(response.control)
            if original_capture is not None:
                checkout_control["source_cart_reference"] = cart_reference
            control, reasons = authorize_revision_checkout(
                client, checkout_control, snapshots[source_message_id].get("text", ""),
                sealed_sources=sealed_sources, source_cart_capture=original_capture,
            )
            if reasons:
                raise _Rollback(",".join(reasons))
            # Recompute from the saved candidate and current sealed source plan
            # before the proposal factory can create any invoice/offer facts.
            from management.services.ig_response_plan import capture_response_plan
            question_plan = capture_response_plan(client, revision=revision, source_cart_capture=cart_capture)
            question_reason = checkout_question_reason(question_plan, response, sealed_sources,
                checkout_cart_binding=control.get("source_cart_binding"))
            if question_reason:
                raise _Rollback(question_reason)
            expected_selector = (compact_checkout_control(control, artifact=cart_reference["artifact"])
                if original_capture is not None else control)
            if len(bound) != 1 or bound[0].get("selector") != expected_selector:
                raise _Rollback("checkout_cart_authority_mismatch")
            payment_request, _reasons = _payment_request(response.control)
            payment_decision = None
            evidence_ids = [source_message_id]
            if payment_request == "prepay":
                payment_decision, reasons = _prepay_policy(client, sealed_sources)
                if reasons:
                    raise _Rollback(",".join(reasons))
                evidence_ids = [payment_decision.evidence_message_id]
            business_before = capture_checkout_business_facts(client)
            prior_deal_watermark = client.deals.aggregate(value=Max("pk"))["value"] or 0
            checkout = create_or_update_proposal(
                client=client, pay_type="online_full", item_specs=control["items"],
                negotiated_total=None, requested_payment_amount=None, allow_promo=True,
                evidence={"message_ids": evidence_ids}, locale=client.language,
                **({key: control[key] for key in ("source_cart_binding", "source_cart_capture", "checkout_owner_scope")}
                   if original_capture is not None else {}),
            )
            if (
                payment_decision is not None
                or (
                    checkout.assisted_checkout_v2
                    and checkout.payment_policy == checkout.PaymentPolicy.FULL_OR_200_COD
                )
            ):
                current_decision, reasons = _prepay_policy(client, sealed_sources)
                if payment_decision is None:
                    payment_decision = current_decision
                if reasons or payment_decision is None or current_decision != payment_decision or not checkout.assisted_checkout_v2 or any(
                    getattr(checkout, field) != expected for field, expected in (
                        ("payment_policy", payment_decision.policy),
                        ("payment_policy_evidence_message_id", payment_decision.evidence_message_id),
                        ("payment_policy_evidence_kind", payment_decision.evidence_kind),
                        ("payment_policy_evidence_revision", payment_decision.evidence_revision),
                        ("payment_policy_evidence_digest", payment_decision.evidence_digest),
                        ("custom_print_full_only", False),
                    )
                ):
                    raise _Rollback("checkout_prepay_policy_unavailable")
            raw, token = IgCheckoutAccessToken.issue(proposal=checkout, kind=IgCheckoutAccessToken.Kind.BOT)
            base = str(getattr(settings, "SITE_BASE_URL", "") or getattr(settings, "BOT_PUBLIC_BASE_URL", "") or "https://twocomms.shop").rstrip("/")
            url = base + reverse("ig_checkout_token_entry", kwargs={"token": raw})
            text = reply_text.rstrip() + "\n\n" + url
            client.refresh_from_db()
            # Offer creation can create an episode; all bindings must reflect the
            # committed candidate, without rewriting immutable generation input.
            business_after = capture_checkout_business_facts(client)
            cart_owner_after = checkout_owner_scope(client) if original_capture is not None else None
            facts = []
            owned_inventory = None
            for old in authority.fact_bindings:
                selector = old["selector"]
                own_cart_rebind = False
                from management.services.ig_revision_authority import CLAIM_INVENTORY_AVAILABILITY
                if old["claim"] == CLAIM_INVENTORY_AVAILABILITY:
                    from management.services.ig_inventory_availability import capture_owned_checkout_inventory, owned_inventory_reference
                    owned_inventory = capture_owned_checkout_inventory(client.pk,
                        selector.get("inventory_availability_capture"), proposal_id=checkout.pk, now=now)
                    if owned_inventory.get("status") != "captured":
                        raise _Rollback(owned_inventory.get("reason") or "checkout_inventory_owned_hold_unavailable")
                    selector = {"inventory_availability_reference": owned_inventory_reference(revision, owned_inventory)}
                    own_cart_rebind = True
                if old["claim"] == CLAIM_CATALOG_CONFIGURATION and original_capture is not None:
                    owned = rebind_checkout_cart_owner(control,
                        before=control["checkout_owner_scope"], after=cart_owner_after, checkout=checkout)
                    if owned is None:
                        raise _Rollback("checkout_cart_owner_rebind_failed")
                    checkout_revision = checkout.revisions.get(revision=checkout.revision)
                    artifact = {"kind": "checkout_revision", "proposal_id": checkout.pk,
                        "checkout_revision_id": checkout_revision.pk, "revision": checkout.revision}
                    reference = compact_checkout_control(owned, artifact=artifact)["source_cart_reference"]
                    persisted, reasons = checkout_artifact_control(client, reference)
                    from management.services.ig_revision_cart_binding import same_checkout_source_capture
                    if (reasons or persisted["items"] != owned["items"] or not same_checkout_source_capture(
                            owned["source_cart_capture"], persisted["source_cart_capture"])):
                        raise _Rollback("checkout_cart_artifact_rebind_failed")
                    selector = compact_checkout_control(persisted, artifact=artifact)
                    own_cart_rebind = True
                fresh = build_revision_authority_bindings(client, claims=(old["claim"],), control=selector, settings_obj=settings_obj)
                if not fresh.ready or not fresh.fact_bindings:
                    raise _Rollback("checkout_post_fact_unavailable")
                current = fresh.fact_bindings[0]
                if current["authority_digest"] != old["authority_digest"] and not own_cart_rebind and not owned_checkout_business_rebind(
                    old, current, before=business_before.get(old["claim"]),
                    after=business_after.get(old["claim"]), checkout=checkout,
                    prior_deal_watermark=prior_deal_watermark,
                ):
                    raise _Rollback("checkout_post_fact_unavailable")
                facts.extend(fresh.fact_bindings)
            # Asset authority is plan-only: never alter the saved generation
            # or accept caller-added bindings before its exact authority guard.
            from management.services.ig_revision_catalog_assets import prepare_catalog_asset_control
            from management.services.ig_revision_authority import CLAIM_CATALOG_ASSET_REFERENCES

            asset_control, asset_reasons = prepare_catalog_asset_control(client, prior, control=response.control)
            if asset_reasons:
                raise _Rollback("checkout_catalog_assets:" + ",".join(asset_reasons))
            if asset_control:
                assets = build_revision_authority_bindings(
                    client, claims=(CLAIM_CATALOG_ASSET_REFERENCES,),
                    control=asset_control, settings_obj=settings_obj,
                )
                if not assets.ready:
                    raise _Rollback("checkout_catalog_assets:" + ",".join(assets.reasons))
                facts.extend(assets.fact_bindings)
            offer = build_revision_authority_bindings(client, claims=(CLAIM_CURRENT_OFFER,), control={"checkout_access_token_id": token.pk}, settings_obj=settings_obj)
            if not offer.ready or offer.offer_bindings[0]["subjects"]["proposal_id"] != checkout.pk:
                raise _Rollback("checkout_post_offer_unavailable")
            from management.services.ig_reply_authority import build_reply_truth_context
            from management.services.ig_reply_truth import validate_reply_truth
            context = build_reply_truth_context(client, control=response.control, server_urls=(url,))
            from management.services.ig_reply_authority import apply_inventory_availability_context
            if owned_inventory is not None:
                context = apply_inventory_availability_context(context, owned_inventory)
            truth = validate_reply_truth(text, context=context)
            if not truth.valid:
                raise _Rollback("checkout_final_truth:" + ",".join(truth.reasons))
            namespace = revision.bundle_snapshot["sources"][0]["source_namespace"]
            prepared = prepare_text_effects(client.igsid, text, provider_namespace=namespace)
            if prepared.error:
                raise _Rollback("checkout_text:" + prepared.error)
            result = plan_revision_effects(
                revision.pk, revision_token, source_message_id=source_message_id,
                settings_id=settings_id, settings_permission_epoch=settings_permission_epoch,
                publication=publication, authority_context_digest=plan_identity,
                effects=(*prior, *prepared.effects), fact_bindings=facts,
                offer_bindings=offer.offer_bindings, fact_checker=check_fact_bindings,
                offer_checker=check_offer_bindings, generation_request_id=request_id,
                generation_model=generation.get("actual_model", ""), now=now,
            )
            if not result.effects or result.reasons:
                raise _Rollback("checkout_plan:" + ",".join(result.reasons))
            return RevisionCheckoutResult(True, result.created, result.effects, checkout.pk)
    except (CheckoutConfigurationError, _Rollback) as exc:
        return RevisionCheckoutResult(reasons=(str(exc),))
    except Exception:
        return RevisionCheckoutResult(reasons=("checkout_preparation_failed",))


def prepare_revision_assisted_checkout(
    revision_id, revision_token, *, source_message_id, settings_id,
    settings_permission_epoch, publication, generation_proposal_digest,
    authority, noncheckout_effects=(), reply_text, source_cart_capture=None, now=None,
):
    """Use the one native assisted quote producer, then the existing outbox."""
    import uuid
    from management.services.ig_assisted_commerce_context import ACTION as assisted_action, CLAIM, capture_assisted_context
    from management.services.ig_assisted_checkout import prepare_assisted_checkout
    from management.services.ig_commercial_episodes import commercial_episode_client_lock
    from management.services.ig_response_cart_fence import capture_source_cart_authority
    from management.services.ig_revision_authority import CLAIM_SOURCE_PREFERENCES
    from management.services.ig_reply_authority import build_reply_truth_context

    if connection.in_atomic_block:
        return RevisionCheckoutResult(reasons=("caller_transaction_active",))
    if not isinstance(authority, RevisionAuthorityBindingSet) or not authority.ready or assisted_action not in authority.allowed_actions:
        return RevisionCheckoutResult(reasons=("assisted_checkout_action_not_authorized",))
    identity = IgCustomerTurnRevision.objects.filter(pk=revision_id).values("client_id").first()
    if identity is None:
        return RevisionCheckoutResult(reasons=("revision_missing",))
    now = now or timezone.now()
    try:
        with commercial_episode_client_lock(identity["client_id"]), transaction.atomic():
            client = IgClient.objects.select_for_update().get(pk=identity["client_id"])
            revision = IgCustomerTurnRevision.objects.select_for_update().select_related("client").get(pk=revision_id, client=client)
            settings_obj = InstagramBotSettings.objects.select_for_update().select_related("active_instruction_publication").get(pk=settings_id)
            proposal = revision.generation_proposal
            if (not generation_proposal_digest or revision.generation_proposal_digest != generation_proposal_digest
                or not isinstance(proposal, Mapping) or _digest(proposal) != generation_proposal_digest):
                raise _Rollback("generation_proposal_mismatch")
            captured = (proposal.get("policy_manifest") or {}).get("instruction_publication") or {}
            if any(captured.get(key) != value for key, value in (("id", publication.publication_id),
                ("version", publication.version), ("hash", publication.snapshot_hash))):
                raise _Rollback("generation_publication_mismatch")
            supplied = _authority_projection(authority)
            if supplied != proposal.get("authority") or assisted_action not in supplied.get("allowed_actions", ()):
                raise _Rollback("generation_proposal_authority_mismatch")
            refs = [row.get("selector", {}).get("assisted_reference") for row in authority.fact_bindings if row.get("claim") == CLAIM]
            if len(refs) != 1 or refs[0] != proposal.get("assisted_offer_reference"):
                raise _Rollback("assisted_generation_reference_mismatch")
            snapshot = next((row for row in revision.bundle_snapshot.get("sources", ()) if row.get("message_id") == source_message_id), None)
            if (not snapshot or snapshot.get("role") != "user"
                or source_message_id not in {row.get("message_id") for row in proposal.get("sources", ())}
                or not revision.sources.filter(message_id=source_message_id, message__client_id=client.pk).exists()):
                raise _Rollback("source_not_in_revision")
            response = _response(proposal)
            _composition(revision, response, reply_text)
            prior = tuple(noncheckout_effects)
            if prior:  # Custom photos require their own native asset capability.
                raise _Rollback("assisted_checkout_catalog_media_unsupported")
            generation = proposal.get("generation") or {}
            request_id = str(generation.get("request_id") or "")
            if not request_id:
                raise _Rollback("generation_request_missing")
            plan_identity = _digest({"boundary": assisted_action, "generation_proposal_digest": generation_proposal_digest,
                "source_message_id": source_message_id, "authority": supplied, "reply_text": reply_text, "noncheckout_effects": prior})
            existing = tuple(revision.delivery_effects.order_by("order_index", "id"))
            if existing:
                if any(row.authority_context_digest != plan_identity or row.generation_request_id != request_id
                    or row.source_message_id != source_message_id or row.plan_digest != existing[0].plan_digest for row in existing):
                    raise _Rollback("checkout_plan_conflict")
                first = existing[0]
                if (first.settings_id_snapshot != settings_id or first.settings_permission_epoch != settings_permission_epoch
                    or first.publication_id != publication.publication_id or first.publication_version != publication.version
                    or first.publication_hash != publication.snapshot_hash):
                    raise _Rollback("checkout_plan_publication_mismatch")
                checked = pre_winner_readiness(revision.pk, revision_token, settings_id=settings_id,
                    settings_permission_epoch=settings_permission_epoch, publication=publication,
                    fact_bindings=first.fact_bindings, offer_bindings=first.offer_bindings,
                    fact_checker=check_fact_bindings, offer_checker=check_offer_bindings, now=now)
                if not checked.ready:
                    raise _Rollback("readiness:" + ",".join(checked.reasons))
                offers = [row for row in first.offer_bindings if row.get("claim") == CLAIM_CURRENT_OFFER]
                if len(offers) != 1:
                    raise _Rollback("checkout_plan_offer_missing")
                return RevisionCheckoutResult(True, False, existing, offers[0]["subjects"]["proposal_id"])
            # This check still uses PRE-owner generation facts. The coordinator
            # commits its quote and POST-owner facts in this same transaction.
            checked = pre_winner_readiness(revision.pk, revision_token, settings_id=settings_id,
                settings_permission_epoch=settings_permission_epoch, publication=publication,
                fact_bindings=authority.fact_bindings, offer_bindings=authority.offer_bindings,
                fact_checker=check_fact_bindings, offer_checker=check_offer_bindings, now=now)
            if not checked.ready:
                raise _Rollback("readiness:" + ",".join(checked.reasons))
            context, reason = capture_assisted_context(client, revision, reference=refs[0], now=now)
            if context is None:
                raise _Rollback(reason)
            capture = source_cart_capture or proposal.get("source_cart_capture") or {}
            _controls, reasons = authorize_revision_checkout(client, response.control, snapshot.get("text", ""),
                sealed_sources=revision.bundle_snapshot["sources"], source_cart_capture=capture,
                source_cart_revision_id=revision.pk, assisted_context=context)
            if reasons:
                raise _Rollback(",".join(reasons))
            business_before = capture_checkout_business_facts(client)
            watermark = client.deals.aggregate(value=Max("pk"))["value"] or 0
            result = prepare_assisted_checkout(client.pk, actor=None, request_id=refs[0]["request_id"],
                decision_id=refs[0]["decision_id"], expected_request_digest=refs[0]["request_digest"],
                expected_decision_digest=refs[0]["decision_digest"],
                operation_id=uuid.uuid5(uuid.NAMESPACE_URL, f"ig-assisted-revision:{revision.pk}:{generation_proposal_digest}"),
                revision_id=revision.pk, settings_id=settings_id,
                verified_selection=context.selection)
            if not result.get("ready") or not result.get("token"):
                raise _Rollback(result.get("reason") or "assisted_checkout_token_missing")
            checkout, url = result["proposal"], result["checkout_url"]
            token = IgCheckoutAccessToken.objects.get(proposal=checkout, token_digest=IgCheckoutAccessToken.digest(result["token"]))
            client.refresh_from_db()
            context, reason = capture_assisted_context(client, revision, reference=refs[0], now=now)
            if context is None:
                raise _Rollback(reason)
            fence = capture_source_cart_authority(client, result["current_capture"], revision=revision)
            if not fence.ready:
                raise _Rollback(fence.reason)
            business_after = capture_checkout_business_facts(client)
            facts = []
            for old in authority.fact_bindings:
                selector = old["selector"]
                owned = old["claim"] in {CLAIM, CLAIM_SOURCE_PREFERENCES}
                if old["claim"] == CLAIM:
                    selector = {"assisted_context": context}
                elif old["claim"] == CLAIM_SOURCE_PREFERENCES:
                    selector = {"source_cart_fence": fence.binding}
                fresh = build_revision_authority_bindings(client, claims=(old["claim"],), control=selector, settings_obj=settings_obj)
                if not fresh.ready or not fresh.fact_bindings:
                    raise _Rollback("checkout_post_fact_unavailable")
                current = fresh.fact_bindings[0]
                if (current["authority_digest"] != old["authority_digest"] and not owned
                    and not owned_checkout_business_rebind(old, current, before=business_before.get(old["claim"]),
                        after=business_after.get(old["claim"]), checkout=checkout, prior_deal_watermark=watermark)):
                    raise _Rollback("checkout_post_fact_unavailable")
                facts.extend(fresh.fact_bindings)
            offer = build_revision_authority_bindings(client, claims=(CLAIM_CURRENT_OFFER,),
                control={"checkout_access_token_id": token.pk}, settings_obj=settings_obj)
            if not offer.ready or offer.offer_bindings[0]["subjects"]["proposal_id"] != checkout.pk:
                raise _Rollback("checkout_post_offer_unavailable")
            text = reply_text.rstrip() + "\n\n" + url
            from management.services.ig_response_control import ValidatedResponse
            truth = context.validate_reply(ValidatedResponse(reply_text=text, controls=response.controls),
                build_reply_truth_context(client, control=response.control, server_urls=(url,)))
            if not truth.valid:
                raise _Rollback("checkout_final_truth:" + ",".join(truth.reasons))
            prepared = prepare_text_effects(client.igsid, text, provider_namespace=snapshot["source_namespace"])
            if prepared.error:
                raise _Rollback("checkout_text:" + prepared.error)
            planned = plan_revision_effects(revision.pk, revision_token, source_message_id=source_message_id,
                settings_id=settings_id, settings_permission_epoch=settings_permission_epoch, publication=publication,
                authority_context_digest=plan_identity, effects=prepared.effects, fact_bindings=facts,
                offer_bindings=offer.offer_bindings, fact_checker=check_fact_bindings, offer_checker=check_offer_bindings,
                generation_request_id=request_id, generation_model=generation.get("actual_model", ""), now=now)
            if not planned.effects or planned.reasons:
                raise _Rollback("checkout_plan:" + ",".join(planned.reasons))
            return RevisionCheckoutResult(True, planned.created, planned.effects, checkout.pk)
    except (CheckoutConfigurationError, _Rollback) as exc:
        return RevisionCheckoutResult(reasons=(str(exc),))
    except Exception:
        return RevisionCheckoutResult(reasons=("assisted_checkout_preparation_failed",))
