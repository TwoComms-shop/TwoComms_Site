"""Source-bound current purpose. An old product or bot CTA grants no intent."""
from __future__ import annotations

import hashlib
import re
from datetime import timedelta, time
from zoneinfo import ZoneInfo

from django.utils import timezone

from management.models import IgConversationRouteDecision, IgCustomerTurnRevision, IgDeal, IgFollowUpTask, IgRevisionDeliveryEffect, IgWebhookInboxEvent, InstagramBotMessage, InstagramBotSettings
from management.services.ig_conversation_routes import conversation_route_reset_floor

VERSION = "turn-purpose.v1"
SALES_RESPONSE_ACTIONS = frozenset({
    "client_configuration_update", "checkout_proposal_create",
    "size_gap_notification_intent", "follow_decision_prepare",
})
_PRICE = re.compile(r"(?:яка|який|яку|скільки|сколько|какая|какую|what|how much).{0,35}(?:цін|цен|кошту|стоит|price|cost)|(?:цін[аиу]|цен[аыу]|price)\s*[?？]|(?:цікавить|интересует)\s+(?:цін|цен)|\bhow much\s*[?？]|\bprice[, ]+please\b", re.I)
# These are addressed requests, not arbitrary mentions of choosing clothing.
# Keep the verb at the start of an own clause and require a bounded target.
_SELECTION_TARGET = re.compile(r"\b(?:футбол\w*|худі|худи|одяг\w*|одежд\w*|світшот\w*|свитшот\w*|розмір\w*|размер\w*|принт\w*|t.?shirts?|shirts?|hoodies?|sweatshirts?|outfits?|clothes|clothing|sizes?)\b", re.I)
_SELECTION_REQUEST = re.compile(
    r"^(?:(?:підберіть|підбери|подберите|подбери|порадьте|порадь|посоветуйте|посоветуй|порекомендуйте|порекомендуй|допоможіть|допоможи|помогите|помоги)\b"
    r"|(?:чи\s+)?(?:можете|можеш|могли\s+б\s+ви|могли\s+бы\s+вы)\s+(?:(?:мені|мне)\s+)?(?:підібрати|подобрать|порадити|посоветовать|порекомендувати|порекомендовать|допомогти|помочь)\b"
    r"|(?:help(?:\s+me)?|choose|pick|recommend|suggest)\b"
    r"|(?:can|could|would)\s+you\s+(?:please\s+)?(?:help(?:\s+me)?|choose|pick|recommend|suggest)\b"
    r"|(?:i\s+(?:need|want|would\s+like)|i['’]d\s+like)\s+(?:your\s+)?help\b)", re.I,
)
_SELECTION_WITHDRAWAL = re.compile(
    r"^(?:(?:я\s+)?не\s+(?:(?:хочу|треба|потрібно|нужно|надо|більше|больше|зараз|сейчас|поки|пока|щоб|чтобы|ви|вы|мені|мне)\s+){0,6}"
    r"(?:підбира\w*|підібра\w*|подбира\w*|подобра\w*|радити|советова\w*|рекомендува\w*|рекомендова\w*|допомага\w*|помога\w*)\b"
    r"|(?:i\s+)?(?:do\s+not|don['’]t|no\s+longer|stop)\s+(?:(?:want|need|you|to|please|help|me|with)\s+){0,6}(?:choose|choosing|pick|picking|recommend|recommending|suggest|suggesting)\b"
    r"|(?:can|could|would)\s+you\s+(?:please\s+)?not\s+(?:choose|pick|recommend|suggest)\b"
    r"|(?:можете|можеш)\s+не\s+(?:підбира\w*|підібра\w*|подбира\w*|подобра\w*)\b"
    r"|(?:підбір|подбор)\s+(?:(?:мені|мне|більше|больше|зараз|сейчас)\s+){0,3}не\s+(?:потріб\w*|нуж\w*)\b)", re.I,
)
_SELECTION_REPORTED = re.compile(r"^(?:реклама|оголошення|объявление|цитата|це\s+цитата|у\s+(?:статті|рекламі|дописі)|в\s+(?:статье|рекламе|посте)|переслан[ео]\s+(?:повідомлення|сообщение)|(?:мені|мне)\s+(?:написали|сказали)|(?:друг|подруга|він|вона|он|она)\s+(?:сказ\w*|напис\w*|попрос\w*)|advert\w*|quote|(?:this\s+)?ad\b|forwarded\s+message|(?:he|she|they|my\s+friend)\s+(?:said|wrote|asked))\b", re.I)
_SELECTION_SELF = re.compile(r"^(?:(?:а|але|но|but)\s+)?(?:мені|мне|для\s+мене|для\s+меня|for\s+me)\b[, ]*", re.I)
_SELECTION_SELF_SERVICE = re.compile(r"\b(?:для\s+себе|для\s+себя|for\s+yourself|your\s+(?:style|outfit))\b", re.I)
_SELECTION_POLITE = re.compile(r"^(?:(?:привіт|вітаю|доброго\s+дня|привет|здравствуйте|hello|hi)[, ]+)?(?:(?:а|але|но|but)\s+)?(?:(?:будь\s+ласка|пожалуйста|please)[, ]+)?", re.I)
_SELECTION_SHORT_STOP = re.compile(r"^(?:(?:ні|нет|no)[, ]+)?(?:не\s+треба|не\s+потрібно|не\s+нужно|не\s+надо|no\s+thanks|not\s+anymore)[, ]*$", re.I)
_SIZE_QUESTION = re.compile(r"^(?:який|какой|what|which)\s+(?:розмір|размер|size)\b(?:\s+\w+){0,4}\s+(?:підій\w*|подойд\w*|fit\w*)\b", re.I)
_PRICE_REQUEST = re.compile(r"^(?:(?:тільки|лише|просто|только|just|only)\s+)?(?:скажіть|підкажіть|скажите|подскажите|tell\s+me)\s+(?:(?:мені|мне|the)\s+)?(?:ціну|цену|price)\b", re.I)
_RETAIL = re.compile(r"(?:хочу|хот[еі]л|можна|можно|want|can i|i would like|i['’]d like).{0,35}(?:купит|купув|покуп|замов|заказ|buy|order)|(?:є|есть|have).{0,30}(?:футболк|худі|худи|t.?shirt|hoodie)", re.I)
_ORDER_IMPERATIVE = re.compile(
    r"(?:^|[.!?;\n])\s*(?:(?:будь ласка|пожалуйста|please)[, ]+)?"
    r"(?:(?:оформлюйте|оформіть|оформляйте|оформите|зробіть|створіть|создайте)[,\s]+"
    r"(?:(?:будь ласка|пожалуйста)[,\s]+)?"
    r"(?:(?:моє|мой|мій|цей|этот|це|это)\s+)?(?:замовлення|заказ)\b"
    r"|(?:place|create|complete)\s+(?:(?:my|the|this|an)\s+)?(?:order|purchase)\b"
    r"|(?:замовляю|заказываю)\b)", re.I,
)
_NEGATED_ORDER = re.compile(
    r"\bне\s+(?:(?:зараз|сейчас|поки|ще|більше|больше|взагалі|вообще|нічого|ничего)\s+)*"
    r"(?:(?:хочу|хотів|хотел|буду|треба|потрібно|надо|нужно|планую|планирую|собираюсь)\s+)?"
    r"(?:(?:зараз|сейчас|поки|ще|більше|больше|взагалі|вообще|нічого|ничего|новий|новый|нове|новое)\s+)*(?:куп\w*|покуп\w*|замов\w*|заказ\w*|оформ\w*|створ\w*|созда\w*)"
    r"|\b(?:do\s+not|don['’]t|will\s+not|won['’]t|would\s+not|wouldn['’]t|never)\s+"
    r"(?:(?:want|wish|like)\s+(?:(?:you\s+)?to\s+)?)?(?:(?:an?|the|my|this)\s+)?(?:buy|order|place|create|complete)\b"
    r"|\bnot\s+(?:ready|going|planning)\s+to\s+(?:buy|order|place|create|complete)\b", re.I,
)
_MEDIA_REQUEST = re.compile(r"(?:надішл|над[іи]шл|пришл|присл|скин|send|share).{0,45}(?:фото|скр[іи]н|зображ|модел|image|photo|screenshot)", re.I)
_NONACTION_GREETING = re.compile(r"^(?:привіт|привет|вітаю|доброго дня|добрий день|добрый день|здравствуйте|hello|hi|дякую|спасибо|thanks|thank you)[!.… ]*$", re.I)
_ELLIPTICAL = re.compile(r"^(?:оверсайз|oversize|regular|класичн[аийі]+|классическ[аийое]+|[xsml]{1,4}|[2-5]xl)[.! ]*$", re.I)
_CTA = re.compile(r"(?:хочете|хочешь|хотите|бажаєте|want to|would you like).{0,35}(?:замов|заказ|куп|order|buy)|(?:модель|розмір|размер|model|size).{0,25}(?:доставк|оплат|payment|delivery)|(?:оформимо|оформить|оформити)\s*(?:замов|заказ)|(?:можу|могу|i can).{0,45}(?:підібрат|подобрат|модел|розмір|размер|замов|заказ|доставк|order|size|model|delivery)|(?:надішліть|пришлите|напишіть|напишите|send me).{0,35}(?:модел|розмір|размер|місто|город|model|size|city)", re.I)
_NEW_SALE_CTA = re.compile(r"(?:хочете|хочешь|хотите|бажаєте|want to|would you like).{0,35}(?:замов|заказ|куп|order|buy)|(?:оформимо|оформить|оформити)\s*(?:замов|заказ)", re.I)
_UNREQUESTED_SELECTION = re.compile(
    r"(?:допомож|помог|помочь|help|підбер|підібр|подбер|подобр|choose|pick).{0,60}"
    r"(?:футболк|худі|худи|одяг|одежд|принт|модел|розмір|размер|t.?shirt|hoodie|clothing|clothes|print|model|size)"
    r"|(?:оберіть|виберіть|выберите|choose|pick).{0,40}(?:модел|розмір|размер|принт|model|size|print)", re.I,
)


def _customer_text(text):
    """Keep an explicit question outside quotes; links themselves prove nothing.

    This bounded fallback does not interpret arbitrary pasted advertisements.
    Accepted source-bound routes remain the semantic classifier.
    """
    value = re.sub(r"(?m)^\s*>[^\n]*(?:\n|$)", " ", str(text or ""))
    value = re.sub(r'''«[^»]*»|“[^”]*”|"[^"\n]*"|(?<!\w)'(?:[^'\n]|(?<=\w)'(?=\w))*'(?!\w)''', " ", value)
    value = re.sub(r"https?://[^\s<>]+", " ", value, flags=re.I).strip()
    # An unfinished quote cannot turn the quoted author's request into ours.
    if value.startswith(('"', "'", "«", "“", ">")):
        return ""
    return value


def _selection_signal(text, *, selection_context=False):
    """Last addressed selection directive, or None when there is no proof.

    Reported/ad introductions constrain subsequent pasted clauses. A marked
    own request can follow them; unmarked verbatim copying is indistinguishable
    from an identical customer request and is outside this lexical fallback.
    """
    value = _customer_text(text)
    reported = bool(_SELECTION_REPORTED.match(value))
    signal = None
    for part in re.split(r"[.!?;\n]+|,\s*(?:але|но|but)\s+", value, flags=re.I):
        clause = part.strip()
        own = _SELECTION_SELF.match(clause)
        if reported and not own:
            continue
        if own:
            clause = clause[own.end():]
        clause = _SELECTION_POLITE.sub("", clause, count=1).strip()
        clause = re.sub(r"[, ]+", " ", clause).strip()
        if _SELECTION_WITHDRAWAL.match(clause) or (selection_context and _SELECTION_SHORT_STOP.fullmatch(clause)):
            signal, selection_context = False, False
            continue
        request = _SELECTION_REQUEST.match(clause)
        tail = clause[request.end():] if request else ""
        target = _SELECTION_TARGET.search(tail)
        if ((_SIZE_QUESTION.match(clause) or (request and target and len(tail[:target.start()].split()) <= 8))
            and not _SELECTION_SELF_SERVICE.search(clause)):
            signal, selection_context = True, True
    return signal


def _selection_history(client, sources, floor):
    """Bounded source evidence only; truncated neutral history grants nothing."""
    if not sources:
        return False, [], False
    latest_at = max(row.provider_created_at or row.created_at for row in sources)
    prior = InstagramBotMessage.objects.filter(
        client=client, role="user", sender_id=client.igsid,
        pk__gte=floor, pk__lt=min(row.pk for row in sources),
        created_at__gte=latest_at-timedelta(hours=24),
    )
    namespaces = {row.provider_namespace for row in sources}
    if len(namespaces) == 1:
        prior = prior.filter(provider_namespace=next(iter(namespaces)))
    if client.reply_permission_epoch:
        prior = prior.filter(revision_sources__revision__permission_epoch=client.reply_permission_epoch).distinct()
    recent = list(prior.order_by("-pk")[:5])
    signal, refs = None, []
    for row in reversed(recent[:4]):
        observed = _selection_signal(row.text, selection_context=signal is True)
        if observed is not None:
            signal, refs = observed, [row.pk] if observed is False else []
    return signal is False, refs, len(recent) > 4 and signal is None


def _purpose(text):
    value = _customer_text(text)
    if _PRICE.search(value) or any(_PRICE_REQUEST.match(part.strip()) for part in re.split(r"[.!?;\n,]+", value)):
        return "price_inquiry"
    selection = _selection_signal(text)
    if selection is True:
        return "requested_selection"
    if selection is False:
        return "selection_withdrawal"
    # A customer imperative can permit checkout discussion, but cannot itself
    # supply the separate product/payment authority required by checkout.
    if not _NEGATED_ORDER.search(value) and (_RETAIL.search(value) or _ORDER_IMPERATIVE.search(value)):
        return "retail"
    return ""


def build_turn_intent(client, revision=None, source_messages=None):
    """Read accepted current routes and owned observed USER messages only.

    Text fallback permits a narrow explicit request. A media-only continuation
    can inherit a recent actual customer question across the bot's request for
    a screenshot; a fresh noncommercial message cannot inherit standing sales.
    """
    floor = conversation_route_reset_floor(client.pk)
    if source_messages is None:
        if revision is not None:
            source_messages = [row.message for row in revision.sources.select_related("message").order_by("ordinal", "id")]
        else:
            source_messages = list(InstagramBotMessage.objects.filter(client=client, role="user", pk__gte=floor).order_by("-pk")[:1])
    sources = [row for row in source_messages if row.client_id == client.pk and row.role == "user" and row.pk >= floor]
    ids = sorted(row.pk for row in sources)
    watermark = max(ids, default=0)
    journal = IgConversationRouteDecision.objects.filter(client=client, reset_floor=floor, watermark_message_id__lte=watermark).order_by("-sequence").first()
    if journal is not None and client.reply_permission_epoch and (journal.source_binding or {}).get("client_permission_epoch") != client.reply_permission_epoch:
        journal = None
    fresh = journal is not None and ((revision is not None and journal.revision_id == revision.pk) or journal.watermark_message_id in ids)
    intents = (journal.interpretation or {}).get("intents", []) if fresh else []
    commercial = [item for item in intents if item.get("kind") in {"catalog", "custom_print", "dtf"} and item.get("operation") != "withdraw"]
    evidence = []
    purpose = ""
    selection_withdrawn, withdrawal_refs, continuity_uncertain = _selection_history(client, sources, floor)
    for row in sources:
        if _NEGATED_ORDER.search(_customer_text(row.text)):
            # A catalog topic can remain standing while the customer refuses
            # ordering. Neither that route nor an earlier bundle phrase grants
            # current sales acts after an explicit observed refusal.
            purpose = "purchase_refusal"
            evidence = []
            continue
        selection = _selection_signal(row.text, selection_context=purpose == "requested_selection")
        if selection is not None:
            selection_withdrawn, withdrawal_refs, continuity_uncertain = not selection, [row.pk] if not selection else [], False
            if not selection:
                purpose, evidence = "selection_withdrawal", []
        observed = _purpose(row.text)
        if observed and observed != "selection_withdrawal":
            purpose = observed
            evidence.append(row.pk)
    if not purpose and sources and not any(str(row.text or "").strip() for row in sources) and not (fresh and not commercial):
        latest_at = max(row.provider_created_at or row.created_at for row in sources)
        previous = list(InstagramBotMessage.objects.filter(client=client, role="user", pk__gte=floor, pk__lt=min(ids), created_at__gte=latest_at-timedelta(hours=24)).order_by("-pk")[:4])
        # Only the immediately preceding meaningful customer statement survives.
        prior = next((row for row in previous if str(row.text or "").strip()), None)
        if prior is not None:
            intervening_reply = InstagramBotMessage.objects.filter(client=client, role__in=("model", "manager"), pk__gt=prior.pk, pk__lt=min(ids)).order_by("-pk").first()
            prior_at = prior.provider_created_at or prior.created_at
            direct_bundle = intervening_reply is None and latest_at - prior_at <= timedelta(minutes=3)
            requested_media = (
                intervening_reply is not None and intervening_reply.role == "model"
                and intervening_reply.status == "done"
                and intervening_reply.send_state not in {"unknown", "sending", "failed"}
                and (intervening_reply.provider_message_id or (intervening_reply.mid and intervening_reply.source in {"webhook", "poll", "poll_history", "echo"}))
                and _MEDIA_REQUEST.search(str(intervening_reply.text or ""))
            )
            if direct_bundle or requested_media:
                purpose = _purpose(prior.text)
                if purpose == "requested_selection" and (selection_withdrawn or continuity_uncertain):
                    purpose = "selection_withdrawal" if selection_withdrawn else ""
                if purpose and purpose != "selection_withdrawal":
                    evidence.append(prior.pk)
    if not purpose and not fresh and journal is not None and not selection_withdrawn and not continuity_uncertain:
        prior_commerce = [item for item in (journal.interpretation or {}).get("intents", ()) if item.get("kind") in {"catalog", "custom_print", "dtf"} and item.get("operation") != "withdraw"]
        # A concrete terse configuration answer can continue an accepted retail
        # route. A greeting/thanks or a fresh community route cannot do so.
        if prior_commerce and any(_ELLIPTICAL.fullmatch(str(row.text or "").strip()) for row in sources):
            prior_at = journal.occurred_at
            latest_at = max(row.provider_created_at or row.created_at for row in sources)
            if latest_at - timedelta(hours=24) <= prior_at <= latest_at:
                purpose = "retail"
                evidence = ids
    # An accepted route identifies a topic, not the sender's request to shop.
    # In particular, recognized apparel in a shared article/photo, a bare URL,
    # or quoted ad must never become purchase permission through model labels.
    # Only the observed requests/continuations above grant retail response acts.
    if not purpose:
        purpose = next((item.get("kind") for item in intents if item.get("operation") != "withdraw"), "unknown")
    acts = ["answer_current_question", "acknowledge_current_topic"]
    if evidence:
        acts += ["retail_consultation"]
        if not selection_withdrawn and not continuity_uncertain:
            acts += ["optional_retail_next_step"]
    cycle_basis = f"{client.pk}:{floor}:{purpose}:{','.join(map(str, sorted(evidence)))}"
    return {"version": VERSION, "purpose": purpose, "commerce_evidence_refs": sorted(evidence),
            "source_message_ids": ids, "route_decision_id": journal.pk if fresh else 0,
            "standing_interest": list(journal.active_intents or []) if journal else [],
            "allowed_response_acts": acts, "uncertainty": "" if evidence or fresh else "current_intent_unproven",
            "cycle_key": hashlib.sha256(cycle_basis.encode()).hexdigest() if evidence else "",
            "source_revision_id": getattr(revision, "pk", 0) or 0, "reset_floor": floor,
            "selection_withdrawn": selection_withdrawn, "selection_withdrawal_refs": withdrawal_refs,
            "selection_continuity_uncertain": continuity_uncertain,
            "source_scope": _accepted_scope(journal, ids) if fresh else {}}


def intent_generation_guidance(decision):
    allowed = "retail_consultation" in decision.get("allowed_response_acts", ())
    restriction = ("The customer withdrew clothing selection. Answer any explicit factual question, but do not offer alternatives, optional sales steps or follow-ups. This is not a price objection or a refusal of an independently requested checkout. "
                   if decision.get("selection_withdrawn") else
                   "Earlier selection permission is unproven in the bounded source history. Answer the current explicit question without optional selection or follow-ups. "
                   if decision.get("selection_continuity_uncertain") else "")
    return (f"Current customer purpose: {decision.get('purpose', 'unknown')}. " + restriction
            + ("Answer the evidenced retail question; any next step is optional. Do not infer order, price objection, budget or online status from silence." if allowed else
               "Respond to the current topic and evidenced service, handoff or opt-out request. Do not introduce clothing selection, a new order, payment, discounts or sales follow-up. Image contents and old product interest are not purchase intent."))


def source_only_noncommercial(decision, source_messages):
    """Known lack of a sender request, before accepting model topic labels."""
    return bool(source_messages) and not decision.get("commerce_evidence_refs") and all(
        not _customer_text(row.text) or _NONACTION_GREETING.fullmatch(_customer_text(row.text))
        for row in source_messages
    )


def validate_turn_response(decision, text, actions=()):
    if ((decision.get("selection_withdrawn") or decision.get("selection_continuity_uncertain"))
        and "retail_consultation" in decision.get("allowed_response_acts", ())):
        # An explicit order retains the independent checkout gate. A factual
        # price question alone cannot reopen selection or optional sales work.
        if (_UNREQUESTED_SELECTION.search(str(text or ""))
            or (decision.get("purpose") != "retail" and (_CTA.search(str(text or "")) or SALES_RESPONSE_ACTIONS.intersection(actions)))):
            return "selection_withdrawal_disallows_sales"
    if "retail_consultation" not in decision.get("allowed_response_acts", ()):
        prose_rule = _NEW_SALE_CTA if decision.get("purpose") == "support" else _CTA
        if (SALES_RESPONSE_ACTIONS.intersection(actions) or prose_rule.search(str(text or ""))
            or (decision.get("purpose") != "support" and _UNREQUESTED_SELECTION.search(str(text or "")))):
            return "current_purpose_disallows_sales"
    return ""


def _accepted_scope(journal, source_ids):
    """Only an accepted interpretation of these exact sources proves scope."""
    if journal is None:
        return {}
    intents = [item for item in (journal.interpretation or {}).get("intents", ())
               if item.get("operation") != "withdraw"]
    evidence = {pk for item in intents for pk in item.get("evidence_message_ids", ())}
    if not intents or not evidence or not evidence.issubset(set(source_ids)):
        return {}
    refs = (getattr(journal, "source_binding", None) or {}).get("source_refs") or {}
    return {"route_decision_id": journal.pk, "source_message_ids": sorted(source_ids),
            "route_kinds": sorted({item["kind"] for item in intents}),
            "commercial_episode_id": refs.get("commercial_episode_id"),
            "line_id": refs.get("line_id") or ""}


def _scope_for_sources(client, source_ids):
    if not source_ids:
        return {}
    journal = IgConversationRouteDecision.objects.filter(
        client=client, watermark_message_id__in=source_ids,
    ).order_by("-sequence").first()
    return _accepted_scope(journal, source_ids)


def _disjoint_scopes(current, old, *, episodes_only=False):
    """Missing scope is not disjoint; chronology alone never closes a debt."""
    if not current or not old:
        return False
    if set(current.get("source_message_ids", ())) & set(old.get("source_message_ids", ())):
        return False
    current_episode, old_episode = current.get("commercial_episode_id"), old.get("commercial_episode_id")
    if current_episode and old_episode and current_episode != old_episode:
        return True
    if episodes_only:
        return False
    # A proven catalog question is independent of a separately interpreted
    # service/community/job discussion. Two retail routes can share a purchase.
    current_kinds, old_kinds = set(current.get("route_kinds", ())), set(old.get("route_kinds", ()))
    return current_kinds == {"catalog"} and bool(old_kinds) and old_kinds.issubset({"support", "community", "employment", "collaboration"})


def _case_scope(client, task):
    payload, context = task.event_payload or {}, task.manager_context or {}
    if (task.reason == "revision_case:paid_fulfillment" and task.deal_id
        and task.deal.client_id == client.pk and context.get("deal_id") == task.deal_id
        and payload.get("deal_id") == task.deal_id):
        scope = {"commercial_episode_id": getattr(getattr(task.deal, "commercial_episode", None), "pk", None),
                 "source_message_ids": [], "route_kinds": ["support"]}
        return "fulfillment", scope
    if task.reason == "revision_case:custom_print":
        ids = [item.get("message_id") for item in context.get("sources", ()) if item.get("message_id")]
        owner = IgCustomerTurnRevision.objects.filter(pk=context.get("latest_revision_id"), client=client).first()
        if (owner is not None and owner.generation_proposal_digest
            and owner.generation_proposal_digest == context.get("generation_proposal_digest")
            and set(owner.sources.values_list("message_id", flat=True)) == set(ids)):
            return "custom_print", _scope_for_sources(client, ids)
    if task.reason.startswith("prize_review:") and payload.get("schema_version") == "ig-prize-case-v1" and context.get("schema_version") == "ig-prize-case-v1":
        ids = {item.get("source_message_id") for item in context.get("evidence", ()) if item.get("source_message_id")}
        ids.update(item.get("source_message_id") for item in context.get("preferences", ()) if item.get("source_message_id"))
        initial_id = payload.get("initial_source_message_id")
        if (initial_id in ids and payload.get("case_kind") == "prize_review"
            and payload.get("programme_id") == context.get("programme_id")
            and payload.get("programme_version") == context.get("programme_version")):
            return "prize", _scope_for_sources(client, ids)
    return "unknown", {}


def _informational_generation_debt(client, task, old, decision, revision):
    """A failed non-action input can stay owed without vetoing a fresh answer.

    This read-only adapter also handles old canonical cases that predate the
    owner/disposition JSON fields. It neither declares coverage nor resolves
    the case. An actual unanswered customer question remains blocking.
    """
    from management.services.ig_turn_revisions import _copied_source_payloads, _digest, _source_payload

    payload, context = task.event_payload or {}, task.manager_context or {}
    failure = payload.get("reason")
    if (old is None or revision is None or old.pk == revision.pk or getattr(old, "active_slot", 1) is not None
        or task.event_key != f"ig-revision-debt:{old.pk}"
        or context.get("case_kind") != "revision_execution_debt"
        or context.get("revision_id") != old.pk or context.get("reason") != failure
        or context.get("automatic_http_retry") is not False
        or context.get("owner", "manager") != "manager"
        or context.get("disposition", "manager_reply") != "manager_reply"
        or failure not in {"generation_failed", "generation_not_started", "generation_result_missing",
                           "generation_outcome_unresolved", "provider_candidates_exhausted", "preparation_expired"}
        or payload.get("effect_ids") != [] or old.generation_proposal_digest or old.delivery_effects.exists()
        or decision.get("purpose") not in {"price_inquiry", "requested_selection"}
        or not decision.get("commerce_evidence_refs") or not confirmed_substantive_reply(revision)):
        return None
    receipts = old.action_receipts or {}
    if (set(receipts) - {"input_decision", "generation_admission", "provider_execution_manifest",
                         "provider_execution_reference", "response_debt", "source_transfer_in", "source_transfer_out"}
        or (receipts.get("input_decision") and receipts["input_decision"].get("origin") != "generate")):
        return None
    sources = list(old.sources.select_related("message").order_by("ordinal", "id")[:33])
    ids = {row.message_id for row in sources}
    current = set(decision.get("source_message_ids", ())) | set(decision.get("commerce_evidence_refs", ()))
    if (not sources or len(sources) > 32 or len(sources) != old.source_count
        or ids != set(payload.get("source_message_ids") or ()) or ids.intersection(current)
        or max(ids) >= min(current, default=0)):
        return None
    for source, frozen in zip(sources, _copied_source_payloads(sources), strict=True):
        row = source.message
        text = str(source.text or "").strip()
        if (row.client_id != client.pk or row.sender_id != client.igsid or row.source != "webhook"
            or row.role != "user" or source.role != "user" or not source.source_namespace
            or row.send_state or source.quick_reply_payload or source.media_part_count or row.attachment_media
            or not (_NONACTION_GREETING.fullmatch(text) or re.fullmatch(r"https?://[^\s<>]+", text, re.I))
            or source.source_digest != _digest({key: value for key, value in frozen.items() if key != "source_digest"})
            or _source_payload(row, previous=source, ordinal=source.ordinal)["source_digest"] != source.source_digest):
            return None
    return {"version": "technical-debt-nonblocking.v1", "task_id": task.pk, "revision_id": old.pk,
            "source_refs": [{"message_id": row.message_id, "source_digest": row.source_digest} for row in sources]}


def purpose_blockers(client, decision, *, revision=None):
    """Return a blocker without resolving or bulk closing manager cases."""
    if decision.get("selection_withdrawn"):
        return "selection_followup_withdrawn"
    if decision.get("selection_continuity_uncertain"):
        return "selection_continuity_unproven"
    current_ids = set(decision.get("source_message_ids", ())) | set(decision.get("commerce_evidence_refs", ()))
    current_scope = decision.get("source_scope") or {}
    cases = IgFollowUpTask.objects.filter(client=client, kind="manager_task").select_related("deal").exclude(status__in=("completed", "cancelled")).exclude(reason__startswith="parcel_reminder:")
    for task in cases:
        payload = task.event_payload or {}
        context = task.manager_context or {}
        if task.reason == "revision_case:execution_debt" and revision is not None:
            old_id = payload.get("revision_id") or context.get("revision_id")
            old = IgCustomerTurnRevision.objects.filter(pk=old_id, client=client).first()
            old_ids = set(payload.get("source_message_ids") or ())
            informational = _informational_generation_debt(client, task, old, decision, revision)
            if informational:
                decision.setdefault("informational_debt_refs", []).append(informational)
                continue
            if old is not None and old_ids and old.pk != revision.pk and max(old_ids) < min(current_ids, default=0):
                old_sources = list(old.sources.select_related("message").order_by("ordinal", "id"))
                exact_sources = {row.message_id for row in old_sources} == old_ids and all(row.message.client_id == client.pk and row.message.role == "user" for row in old_sources)
                greeting_only = exact_sources and all(_NONACTION_GREETING.fullmatch(str(row.message.text or "").strip()) for row in old_sources)
                # Resolution is producer-written and bound to the exact debt,
                # source set, successor and its confirmed substantive receipt.
                resolution = (old.action_receipts or {}).get("response_debt_resolution") or {}
                transferred = exact_sources and resolution.get("outcome") in {"fulfilled", "transferred", "superseded"} and resolution.get("successor_revision_id") == revision.pk and set(resolution.get("source_message_ids") or ()) == old_ids
                confirmed_successor = transferred and revision.delivery_effects.filter(group="substantive_text", state="sent").exists() and not revision.delivery_effects.filter(group="substantive_text").exclude(state="sent").exists()
                if (greeting_only or confirmed_successor) and not old.delivery_effects.filter(state__in=("unknown", "provider_started", "claimed")).exists():
                    continue  # preserved case; exact nonaction/transfer evidence
                if exact_sources and old.pk != revision.pk:
                    old_scope = _scope_for_sources(client, old_ids)
                    same_turn = getattr(old, "turn_id", None) and getattr(old, "turn_id", None) == getattr(revision, "turn_id", None)
                    if not same_turn and _disjoint_scopes(current_scope, old_scope):
                        continue  # independent purpose; debt remains visible
        case_kind, case_scope = _case_scope(client, task)
        if case_kind != "unknown" and _disjoint_scopes(current_scope, case_scope, episodes_only=True):
            continue
        return "pending_manager_case"
    from django.db.models import Q
    for deal in IgDeal.objects.filter(client=client).filter(Q(payment_truth="pending") | Q(payment_projection__truth="pending")):
        if not _disjoint_scopes(current_scope, {"commercial_episode_id": getattr(getattr(deal, "commercial_episode", None), "pk", None)}, episodes_only=True):
            return "payment_verification_pending"
    unknowns = IgRevisionDeliveryEffect.objects.filter(revision__client=client, state__in=("unknown", "provider_started")).select_related("revision")
    for effect in unknowns:
        old = effect.revision
        old_ids = list(old.sources.values_list("message_id", flat=True))
        same_turn = revision is None or old.turn_id == getattr(revision, "turn_id", None)
        same_snapshot = revision is not None and old.snapshot_digest and old.snapshot_digest == revision.snapshot_digest
        if same_turn or same_snapshot or current_ids.intersection(old_ids) or not _disjoint_scopes(current_scope, _scope_for_sources(client, old_ids)):
            return "unknown_delivery_pending"
    return ""


def ordinary_next_send_at(candidate):
    local = candidate.astimezone(ZoneInfo("Europe/Kyiv"))
    if local.time() < time(10):
        local = local.replace(hour=10, minute=0, second=0, microsecond=0)
    elif local.time() >= time(20, 30):
        local = (local + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    return local.astimezone(candidate.tzinfo)


LEGACY_ORDINARY_REASONS = frozenset({"first_reply_silence", "price_quoted_silence", "missing_customer_size", "thinking_hesitation"})


def confirmed_substantive_reply(revision):
    """Read exact persisted answer receipts; no locks or provider operations.

    Callers that mutate a task already hold their normal settings/client locks.
    A source question or a model winner alone is insufficient delivery proof.
    """
    from management.services.ig_revision_outbox import _digest

    effects = list(IgRevisionDeliveryEffect.objects.filter(revision_id=revision.pk).order_by("order_index", "id")[:17])
    text = [row for row in effects if row.group == "substantive_text"]
    if (not text or len(effects) > 16 or len(text) != text[0].part_count
        or [row.part_index for row in text] != list(range(len(text)))
        or any(row.state in {"planned", "claimed", "provider_started", "unknown"} for row in effects)):
        return []
    source_ids = {row["message_id"] for row in (revision.bundle_snapshot or {}).get("sources", ())}
    if any(row.actor != "bot" or row.purpose != "normal_reply"
           or row.state != "sent" or not row.provider_message_id or row.terminal_at is None
           or row.part_count != len(text) or row.plan_digest != text[0].plan_digest
           or row.revision_snapshot_digest != revision.snapshot_digest or row.source_message_id not in source_ids
           or _digest(row.payload) != row.payload_digest for row in text):
        return []
    return text


def _followup_answer_binding_current(task, revision):
    payload = task.event_payload or {}
    receipt = (revision.action_receipts or {}).get("normal_followups") or {}
    text = confirmed_substantive_reply(revision)
    ids = [row.pk for row in text]
    if (not text or payload.get("sent_effect_ids") != ids or receipt.get("sent_effect_ids") != ids
        or receipt.get("task_id") != task.pk or receipt.get("snapshot_digest") != revision.snapshot_digest
        or receipt.get("plan_digest") != text[0].plan_digest
        or payload.get("sent_reply_anchor") != max(row.terminal_at for row in text).isoformat()):
        return False
    return all(row.settings_id_snapshot == payload.get("settings_id")
               and row.settings_permission_epoch == payload.get("settings_permission_epoch")
               and row.client_permission_epoch == payload.get("client_permission_epoch")
               and row.publication_id == payload.get("publication_id")
               and row.publication_hash == payload.get("publication_hash") for row in text)


def revalidate_followup_intent(task, now=None):
    payload = task.event_payload or {}
    if payload.get("origin") != "ordinary_intent_followup":
        if task.trigger == "time" and task.reason in LEGACY_ORDINARY_REASONS:
            # Old ladders lack source-bound budget and sent-answer identity.
            # They cannot safely be upgraded or replayed against old clients.
            return "legacy_ordinary_followup_unbound"
        return ""
    now = now or timezone.now()
    client = task.client
    if client.hidden_at or client.is_blocked or client.bot_paused or client.manager_takeover or client.privacy_erasure_started_at:
        return "followup_permission_changed"
    revision = IgCustomerTurnRevision.objects.filter(pk=payload.get("revision_id"), client=client).first()
    if revision is None:
        return "followup_source_unavailable"
    from management.services.ig_revision_outbox import _digest
    if (not revision.snapshot_digest or _digest(revision.bundle_snapshot) != revision.snapshot_digest
        or revision.snapshot_digest != payload.get("snapshot_digest")
        or payload.get("source_message_ids") != [row["message_id"] for row in revision.bundle_snapshot.get("sources", ())]):
        return "followup_source_changed"
    if not _followup_answer_binding_current(task, revision):
        return "followup_answer_receipts_changed"
    settings_obj = InstagramBotSettings.objects.select_related("active_instruction_publication").filter(pk=payload.get("settings_id")).first()
    if settings_obj is None or not settings_obj.is_enabled or settings_obj.reply_permission_epoch != payload.get("settings_permission_epoch"):
        return "followup_settings_changed"
    publication = settings_obj.active_instruction_publication
    if publication is None or publication.pk != payload.get("publication_id") or publication.snapshot_hash != payload.get("publication_hash"):
        return "followup_publication_changed"
    from management.services.ig_permission_transitions import permission_transition_blocks
    if permission_transition_blocks(settings_id=settings_obj.pk, client_id=client.pk):
        return "permission_transition_pending"
    if IgWebhookInboxEvent.objects.filter(customer_igsid=client.igsid, decision__in=("accepted", "blocked"), processed_at__isnull=True).exists():
        return "pending_inbound"
    from management.services.bot_followups import _client_allows_followup
    allowed, reason = _client_allows_followup(client, deal=task.deal, kind=task.kind)
    if not allowed:
        return reason
    latest = InstagramBotMessage.objects.filter(client=client, role="user").order_by("-pk").first()
    if latest and latest.pk > max(payload.get("source_message_ids") or [0]):
        return "new_customer_statement"
    decision = build_turn_intent(client, revision)
    if decision.get("cycle_key") != payload.get("cycle_key") or decision.get("purpose") != payload.get("purpose"):
        return "followup_purpose_changed"
    if client.reply_permission_epoch != payload.get("client_permission_epoch"):
        return "followup_permission_changed"
    allowed_slot = ordinary_next_send_at(now)
    if allowed_slot > now + timedelta(seconds=1):
        return "ordinary_quiet_hours"
    if task.meta_window_deadline is None or now >= task.meta_window_deadline:
        return "meta_window_closed"
    if client.current_product_id != payload.get("product_id"):
        return "followup_product_changed"
    return purpose_blockers(client, decision, revision=revision)
