"""Source-bound unanswered date questions; this capture grants no timing facts."""
import re

from django.utils import timezone


_DATE = r"(?:дат\w*|термін\w*|срок\w*|deadline|date|timing|guarantee|гарант\w*)"
_FEASIBILITY = re.compile(r"встиг\w*|успе\w*|\b(?:can|could|will|would)\b[^.!?\n]{0,60}\b(?:ship|dispatch|send|arrive|deliver|receive)\w*", re.I)
_REPORTED = re.compile(r"\b(?:said|says|told|wrote|asked|according|сказ\w*|каже|казав\w*|говор\w*|напис\w*|питав\w*|спрос\w*)\b", re.I)


def explicit_feasibility_question(hint):
    """A question about a requested date, not an ordinary wanted-by preference."""
    from management.services.ig_fulfillment_requirements import _customer_text
    raw = hint.get("raw_quote") or ""
    text, hypothetical = _customer_text(raw)
    return bool(hint.get("request_detected") and (hint.get("deadline_date")
            or hint.get("date_status") == "needs_clarification" and hint.get("deadline_raw"))
        and not hint.get("deadline_hard") and not hypothetical and not _REPORTED.search(text)
        and ("?" in text or re.search(r"\b(?:чи|ли|can|could|will|would|встигнете|успеете)\b", text, re.I))
        and _FEASIBILITY.search(text))


def _own_release(text):
    from management.services.ig_fulfillment_requirements import _affirmative_deadline_withdrawal, _customer_text
    if _affirmative_deadline_withdrawal(text):
        return True
    value, hypothetical = _customer_text(text)
    if hypothetical or "?" in text or _REPORTED.search(text) or re.search(r'["«»“”]', text):
        return False
    for clause in re.split(r"[.!?;,\n]+|\b(?:але|но|but)\b", value, flags=re.I):
        acceptance = re.search(r"\b(?:погоджую\w*|згод\w*|приймаю|соглас\w*|принимаю|(?:i|we)\s+accept|(?:i|we)\s+agree)\b", clause, re.I)
        if (not acceptance or re.search(r"\b(?:не|not|never|don't|don’t)\s+(?:\w+\s+){0,3}$", clause[max(0, acceptance.start()-60):acceptance.start()], re.I)
            or re.search(r"\b(?:цін\w*|цен\w*|зниж\w*|скид\w*|price|discount|розмір\w*|размер\w*|кольор\w*|цвет\w*|size|colou?r)\b", clause, re.I)):
            continue
        if re.search(_DATE, clause, re.I) and re.search(
            r"без\s+гарант\w*|не\s+(?:гарант\w*|підтвердж\w*|подтвержд\w*)|without\s+(?:a\s+)?(?:date\s+)?guarantee|unconfirmed|uncertain|not\s+guaranteed", clause, re.I):
            return True
    return False


def _pure_feasibility_source(hint):
    """Only a date question may be consumed as a whole information obligation."""
    from management.services.ig_fulfillment_requirements import _customer_text
    text, hypothetical = _customer_text(hint["raw_quote"])
    if hypothetical:
        return False
    clauses = re.split(r"(?<=[!?;\n])|(?<=\.)(?!\d)", text)
    clauses = [clause for clause in clauses if clause.strip(" .!?;\n")]
    from management.services.ig_commerce_turns import parse_turn
    from management.services.ig_response_plan import _requested_topics
    return bool(clauses and all(explicit_feasibility_question({**hint, "raw_quote": clause})
        and set(_requested_topics(clause, parse_turn(clause).info_topics)) <= {"info:shipping", "info:dispatch_timing", "info:question"}
        for clause in clauses))


def _contextual_question_release(request, cart, settings_obj, *, after_source_id, now, _answer_id=None):
    from management.models import InstagramBotMessage, IgRevisionDeliveryEffect
    from management.services import ig_fulfillment_reviews as reviews
    from management.services.ig_fulfillment_checkout import _deadline_scope_matches
    from management.services.ig_purpose_sources import owned_source
    from management.services.ig_delivery_receipts import normalize_provider_message_id
    from management.services.ig_revision_outbox import _digest

    cutoff = (cart.get("source_watermark") or {}).get("message_id")
    if type(cutoff) is not int or cutoff <= after_source_id or not reviews._verified(request, "request"):
        return 0, False
    if _answer_id is None:
        rows = list(InstagramBotMessage.objects.filter(client_id=request.client_id, sender_id=request.client.igsid,
            role="user", source__in=("webhook", "poll"), provider_namespace=cart["scope"]["source_namespace"],
            pk__gt=after_source_id, pk__lte=cutoff,
            text__iregex=r"^\s*(?:не\s+критична(?:\s+дата)?|не\s+критично|not\s+critical|так\s*,?\s*підходить|да\s*,?\s*подходит|yes\s*,?\s*that\s+works)[,.!\s]*$").exclude(status__in=("failed", "ignored")).order_by("-pk")[:17])
        if len(rows) > 16:
            return 0, False
        for row in rows:
            text = re.sub(r"[,.!\s]+", " ", row.text.strip().casefold()).strip()
            if text not in {"не критична", "не критично", "не критична дата", "not critical", "так підходить", "да подходит", "yes that works"}:
                continue
            released = _contextual_question_release(request, cart, settings_obj,
                after_source_id=after_source_id, now=now, _answer_id=row.pk)
            if released[0]:
                return released
        return 0, False
    if type(_answer_id) is not int or not after_source_id < _answer_id <= cutoff:
        return 0, False
    answer = InstagramBotMessage.objects.filter(pk=_answer_id, client_id=request.client_id,
        role="user", sender_id=request.client.igsid, source__in=("webhook", "poll"),
        provider_namespace=cart["scope"]["source_namespace"]).exclude(status__in=("failed", "ignored")).first()
    if not answer or answer.quick_reply_payload or answer.attachment_media or answer.attachments not in (None, "", "[]"):
        return 0, False
    text = re.sub(r"[,.!\s]+", " ", answer.text.strip().casefold()).strip()
    polarity = ("criticality" if text in {"не критична", "не критично", "не критична дата", "не критично", "not critical"}
        else "uncertainty" if text in {"так підходить", "да подходит", "yes that works"} else "")
    if not polarity:
        return 0, False
    frozen = answer.revision_sources.select_related("revision").order_by("-revision_id").first()
    if not frozen or not owned_source(frozen.revision, answer, settings_obj, require_head=False)[0]:
        return 0, False
    effect = IgRevisionDeliveryEffect.objects.filter(revision__client_id=request.client_id,
        source_message_id__lt=answer.pk, actor="bot", purpose="normal_reply").select_related(
            "revision", "source_message").order_by("-pk").first()
    if (not effect or effect.state != "sent" or effect.kind != "text"
        or effect.part_count != 1 or effect.part_index != 0
        or effect.revision.delivery_effects.count() != 1
        or not normalize_provider_message_id(effect.provider_message_id)
        or not effect.terminal_at or effect.terminal_at > now
        or not answer.provider_created_at or not effect.source_message.provider_created_at
        or not effect.source_message.provider_created_at <= effect.terminal_at <= answer.provider_created_at <= now
        or effect.provider_namespace != answer.provider_namespace
        or effect.recipient_igsid != request.client.igsid
        or effect.settings_id_snapshot != settings_obj.pk
        or effect.settings_permission_epoch != settings_obj.reply_permission_epoch
        or effect.client_permission_epoch != request.client.reply_permission_epoch
        or not effect.plan_digest or effect.payload_digest != _digest(effect.payload)
        or effect.revision_snapshot_digest != effect.revision.snapshot_digest
        or _digest(effect.revision.bundle_snapshot) != effect.revision.snapshot_digest
        or effect.payload.get("recipient") != {"id": request.client.igsid}
        or (answer.reply_to_provider_message_id and answer.reply_to_provider_message_id != effect.provider_message_id)):
        return 0, False
    if not owned_source(effect.revision, effect.source_message, settings_obj, require_head=False)[0]:
        return 0, False
    if InstagramBotMessage.objects.filter(client_id=request.client_id, role="user",
            pk__gt=effect.source_message_id, pk__lt=answer.pk).exists():
        return 0, False
    body = str((effect.payload.get("message") or {}).get("text") or "")
    last = InstagramBotMessage.objects.filter(client_id=request.client_id, role__in=("model", "manager"),
        pk__lt=answer.pk).order_by("-pk").first()
    if (not last or last.source != "revision_reply" or last.send_state != "sent"
        or last.text != body or (last.provider_message_id or last.mid) != effect.provider_message_id):
        return 0, False
    clauses = [part.strip() for part in re.split(r"(?<=[!?;\n])|(?<=\.)(?!\d)", body) if part.strip()]
    questions = [part for part in clauses if part.endswith("?")]
    if len(questions) != 1:
        return 0, False
    question = questions[0]
    from management.services.ig_fulfillment_requirements import extract_requirements, _NUMERIC
    hints = [hint for hint in request.envelope.get("customer_requirements") or []
        if explicit_feasibility_question(hint)]
    dates = {hint["deadline_date"] for hint in hints}
    # 'Will you manage to dispatch' has a legacy mixed arrival/dispatch hint;
    # bind the literal timing topic, not that coarse intent classification.
    intents = {topic for hint in hints for topic, pattern in (
        ("dispatch", r"відправ\w*|отправ\w*|\b(?:dispatch|ship|send)\w*"),
        ("arrival", r"отрима\w*|получ\w*|достав\w*|\b(?:arriv|receiv|deliver)\w*"))
        if re.search(pattern, hint.get("raw_quote") or "", re.I)}
    if len(dates) != 1 or len(intents) != 1 or not intents <= {"dispatch", "arrival"}:
        return 0, False
    # A repeated numeric date corroborates its preceding referent; it cannot
    # replace a refund/arrival topic with an unscoped date-only question.
    position = clauses.index(question)
    previous = clauses[position - 1] if position else ""
    preceding_date = bool(_NUMERIC.search(previous) or re.search(
        r"\b(?:дат\w*|date|термін\w*|срок\w*|timing)\b", previous, re.I))
    context = previous if preceding_date else question
    context_hint = extract_requirements(context, provider_timestamp=effect.terminal_at, now=effect.terminal_at)
    if context_hint.get("deadline_date") not in dates or context_hint.get("date_status") not in {"resolved", "past"}:
        return 0, False
    topic = context_hint.get("intent")
    if topic not in intents:
        return 0, False
    if _NUMERIC.search(question):
        question_hint = extract_requirements(question, provider_timestamp=effect.terminal_at, now=effect.terminal_at)
        if (question_hint.get("deadline_date") not in dates
            or question_hint.get("date_status") not in {"resolved", "past"}
            or question_hint.get("intent") not in {"unknown", topic}):
            return 0, False
    # Closed date-context vocabulary rejects refund dates, additional facets,
    # named products and unsupported beneficiaries rather than guessing scope.
    from management.services.ig_recipient_planning import RECIPIENT_WORDS
    recipients = {str(row.get("recipient_id") or "self") for row in request.envelope.get("lines") or [] if row.get("target")}
    if len(recipients) != 1:
        return 0, False
    value = context.casefold()
    for word, role in RECIPIENT_WORDS.items():
        if re.search(r"\b(?:для|for)\s+" + re.escape(word) + r"\b", value):
            if recipients != {role}:
                return 0, False
            value = re.sub(r"\b(?:для|for)\s+" + re.escape(word) + r"\b", " ", value)
    value = _NUMERIC.sub(" ", value)
    context_words = set(re.findall(r"\w+", value))
    allowed = set("бажана дата дати ця цієї ця цієї відправки відправлення отправки отримання получения доставки поки ще не підтверджена підтверджено подтверждена підтверджений чи для вас вам критична критично важлива важлива ли эта даты дата пока вы отправка получение desired date this that shipping dispatch arrival delivery not yet confirmed is the for you critical important".split())
    if not context_words <= allowed or (re.search(r"\b(?:вас|вам|you)\b", context, re.I) and recipients != {"self"}):
        return 0, False
    # Positive grammar: a negated proposition or conjunction is never turned
    # into acceptance merely because it contains the word 'agree'.
    q = _NUMERIC.sub("DATE", question.casefold()).strip()
    critical = re.fullmatch(r"(?:чи\s+)?(?:ця\s+)?дата(?:\s+DATE)?(?:\s+для\s+вас)?\s+(?:критична|важлива)\s*\?", q)
    uncertain = re.fullmatch(r"чи\s+погоджуєтесь\s+оформлювати\s+без\s+гарантії\s+(?:цієї\s+)?дати\s*\?", q)
    if ((polarity == "criticality" and not critical)
        or (polarity == "uncertainty" and not uncertain)):
        return 0, False
    historical = (effect.revision.generation_proposal or {}).get("source_cart_capture") or {}
    if (not historical.get("coverage_complete") or historical.get("status") != "captured"
        or not _deadline_scope_matches(request, historical) or not _deadline_scope_matches(request, cart)):
        return 0, False
    # The negative requirement may predate policy publication; the SENT
    # question above must still have the current global and client epochs.
    if (request.envelope.get("settings_id") != settings_obj.pk
        or request.envelope.get("client_epoch") != request.client.reply_permission_epoch):
        return 0, False
    def targets(lines):
        return {row["line_id"]: (row.get("recipient_id", "self"),
            {key: value for key, value in row["target"].items() if key != "quantity"})
            for row in lines if row.get("target")}
    old_cutoff = (historical.get("source_watermark") or {}).get("message_id")
    if type(old_cutoff) is not int or old_cutoff > effect.source_message_id:
        return 0, False
    original = targets(request.envelope.get("lines") or [])
    old = targets(reviews._lines(historical, old_cutoff, effect.terminal_at))
    current = targets(reviews._lines(cart, cutoff, now))
    if not original or any(old.get(key) != value or current.get(key) != value for key, value in original.items()):
        return 0, False
    # Without persisted case metadata, only one original case can own the
    # delivered date question. Do not resolve the latest case by proximity.
    candidates = list(reviews.Request.objects.select_related("client", "source").filter(
        client_id=request.client_id, source_id__gte=historical["scope"]["reset_floor"],
        source_id__lte=old_cutoff, created_at__lte=effect.terminal_at,
        envelope__scope="cart", envelope__source_namespace=answer.provider_namespace)[:17])
    if len(candidates) > 16:
        return 0, False
    matching = [row.pk for row in candidates if reviews._verified(row, "request")
        and _deadline_scope_matches(row, historical)
        and targets(row.envelope.get("lines") or [])
        and all(old.get(key) == value for key, value in targets(row.envelope.get("lines") or []).items())
        and any(explicit_feasibility_question(hint) for hint in row.envelope.get("customer_requirements") or [])]
    return (answer.pk, True) if matching == [request.pk] else (0, False)


def _released(request, cart, settings_obj, *, after_source_id, now):
    from management.models import InstagramBotMessage
    from management.services.ig_purpose_sources import owned_source
    from management.services.ig_fulfillment_checkout import _deadline_requirement_update_applies
    cutoff = (cart.get("source_watermark") or {}).get("message_id")
    rows = InstagramBotMessage.objects.filter(client_id=request.client_id, sender_id=request.client.igsid,
        role="user", source__in=["webhook", "poll"], provider_namespace=cart["scope"]["source_namespace"],
        pk__gt=after_source_id, pk__lte=cutoff, text__iregex=_DATE).exclude(status="failed").order_by("-pk")[:17]
    rows = list(rows)
    if len(rows) > 16:
        return 0, False
    for row in rows:
        if not _own_release(row.text):
            continue
        frozen = row.revision_sources.select_related("revision").order_by("-revision_id").first()
        proof = owned_source(frozen.revision, row, settings_obj, require_head=False)[0] if frozen else None
        if proof and _deadline_requirement_update_applies(request, row, cart, now=now):
            clauses = [clause for clause in re.split(r"(?<=[!?;\n])|(?<=\.)(?!\d)", row.text) if clause.strip(" .!?;\n")]
            return row.pk, all(_own_release(clause) for clause in clauses)
    return _contextual_question_release(request, cart, settings_obj, after_source_id=after_source_id, now=now)


def _answered_by_delivered_dispatch(request, hints, current_lines, *, now, settings_obj):
    """An existing typed, delivered dispatch answer may close dispatch questions.

    Delivery/arrival feasibility cannot be inferred from dispatch. Nothing from
    this check is exposed as positive timing authority for the new checkout.
    """
    from datetime import date, timedelta
    from django.utils.dateparse import parse_datetime
    from zoneinfo import ZoneInfo
    from management.models import IgFollowUpTask
    from management.services import ig_fulfillment_reviews as reviews
    from management.services.ig_fulfillment_delivery import render_task_update
    from management.services.ig_delivery_receipts import normalize_provider_message_id
    from management.services.ig_fulfillment_requirements import _CLOCK, _customer_text
    # Negative retention never refreshes an old positive timing decision.
    if (settings_obj.pk != request.envelope.get("settings_id")
        or settings_obj.reply_permission_epoch != request.envelope.get("settings_epoch")):
        return False
    if any(not hint.get("deadline_date") or hint.get("date_status") == "needs_clarification" for hint in hints):
        return False
    if any(re.search(r"отрима\w*|получ\w*|достав\w*|\b(?:arriv|receiv|deliver)\w*", hint["raw_quote"], re.I)
            for hint in hints):
        return False
    decision = reviews.Decision.objects.filter(request=request, created_at__lte=now).order_by("-version").first()
    if not decision or reviews._decision_reason(request, decision, now=now):
        return False
    original = {row["line_id"]:row for row in request.envelope["lines"]}
    current = {row["line_id"]:row for row in current_lines}
    if any(line_id not in current or current[line_id].get("target") != row.get("target")
            for line_id,row in original.items()):
        return False
    for row in decision.envelope.get("lines") or []:
        timing = row.get("dispatch") or {}
        anchor = parse_datetime(str(timing.get("anchor_at") or ""))
        high = timing.get("max_days")
        if (row.get("availability") != "available" or not anchor or timezone.is_naive(anchor)
            or timing.get("anchor") or type(high) is not int or not 0 <= high <= 90
            or timing.get("unit") not in {"calendar", "business"}):
            return False
        latest = anchor.astimezone(ZoneInfo("Europe/Kyiv")).date()
        remaining = high
        while remaining:
            latest += timedelta(days=1)
            if timing["unit"] == "calendar" or latest.weekday() < 5:
                remaining -= 1
        if any(latest > date.fromisoformat(hint["deadline_date"]) or (
                latest == date.fromisoformat(hint["deadline_date"])
                and any(match.group(2) is not None or match.group(3)
                    for match in _CLOCK.finditer(_customer_text(hint["raw_quote"])[0]))) for hint in hints):
            return False
    update = IgFollowUpTask.objects.filter(client_id=request.client_id, kind="fulfillment", reason=reviews.UPDATE_REASON,
        event_key=f"ig-fulfillment-update:{request.pk}:{decision.version}").first()
    expected = {"origin": reviews.UPDATE_REASON, "request_id": str(request.pk), "request_digest": request.envelope_hmac,
        "decision_id": str(decision.pk), "decision_version": decision.version, "decision_digest": decision.envelope_hmac}
    projection = {"current_request": {**request.envelope, "id": str(request.pk)},
        "decision": {**decision.envelope, "id": str(decision.pk)}}
    return bool(update and update.event_payload == expected and update.status == "sent" and update.attempt_count == 1
        and normalize_provider_message_id(update.provider_message_id) and not update.claim_token
        and update.message_text and update.sent_at and decision.created_at <= update.sent_at <= now
        and update.message_text == render_task_update(update, projection))


def capture_fulfillment_question_debt(client, cart, *, now=None, include_resolved=False):
    """Retain only an unanswered own question on the same exact garments.

    Quantity changes do not answer a date question. This negative requirement
    never reuses an old answer, physical availability, price or shipment right.
    Ordinary carts pay only one bounded indexed request lookup.
    """
    from management.services import ig_fulfillment_reviews as reviews
    from management.services.ig_fulfillment_checkout import _deadline_scope_matches
    now = now or timezone.now()
    scope = cart.get("scope") or {}
    cutoff = (cart.get("source_watermark") or {}).get("message_id")
    if (cart.get("status") != "captured" or not cart.get("coverage_complete")
        or scope.get("client_id") != client.pk or type(cutoff) is not int
        or type(scope.get("reset_floor")) is not int or not scope.get("source_namespace")):
        return {}
    requests = list(reviews.Request.objects.select_related("client", "source").filter(client_id=client.pk,
        source_id__gte=scope["reset_floor"], source_id__lte=cutoff, created_at__lte=now,
        envelope__scope="cart", envelope__source_namespace=scope["source_namespace"]
        ).order_by("-source_id", "-created_at", "pk")[:16])
    current_lines, resolved_ids, resolved_release_ids = None, set(), set()
    for request in requests:
        if not reviews._verified(request, "request") or not _deadline_scope_matches(request, cart):
            continue
        source_ids = {proof.get("message_id") for proof in request.envelope.get("sources") or []}
        hints = [hint for hint in request.envelope.get("customer_requirements") or []
            if hint.get("source_id") in source_ids and explicit_feasibility_question(hint)]
        if not hints:
            continue
        settings_obj = reviews._settings()
        reason = reviews._current_reason(request, settings_obj, now=now, current_source_cart=cart, _deadline_hold=True, _negative_requirement_read=True)
        if reason not in {"", "selection_changed", "deadline_continuation_bound"}:
            continue
        if current_lines is None:
            current_lines = reviews._lines(cart, cutoff, now)
        def garment(row):
            return (row.get("line_id"), row.get("recipient_id", "self"),
                {key:value for key,value in (row.get("target") or {}).items() if key != "quantity"})
        previous = [garment(row) for row in request.envelope.get("lines") or [] if row.get("target")]
        matching = [row["line_id"] for row in current_lines if row.get("target") and garment(row) in previous]
        if not matching:
            continue
        released_id, pure_release = _released(request, cart, settings_obj,
            after_source_id=min(hint["source_id"] for hint in hints), now=now)
        if released_id and pure_release:
            resolved_release_ids.add(released_id)
        resolved_ids.update(hint["source_id"] for hint in hints
            if released_id and hint["source_id"] < released_id and _pure_feasibility_source(hint))
        hints = [hint for hint in hints if not released_id or hint["source_id"] > released_id]
        if not hints or _answered_by_delivered_dispatch(request, hints, current_lines, now=now, settings_obj=settings_obj):
            continue
        # A typed timing decision belongs to its original source/cart owner.
        # Do not infer a positive answer from task closure or manager prose.
        return {"version": 1, "request_id": str(request.pk), "request_digest": request.envelope_hmac,
            "source_message_ids": sorted({hint.get("source_id", request.source_id) for hint in hints}),
            "line_ids": matching, "deadline_dates": sorted({hint["deadline_date"] for hint in hints if hint.get("deadline_date")}),
            "date_status": "needs_clarification" if any(hint.get("date_status") == "needs_clarification" for hint in hints) else "resolved",
            "state": "unanswered_feasibility", "checkout": "blocked", "next_step": "clarify_date_importance_or_accept_uncertainty"}
    return {"state": "resolved_by_customer", "source_message_ids": sorted(resolved_ids),
        "release_source_message_ids": sorted(resolved_release_ids)} if include_resolved and resolved_ids else {}


def question_purchase_handled(requirement, response):
    """A specific pending-date clarification handles purchase, never the answer."""
    if not requirement or response.control.get("paylink") or response.control.get("payment"):
        return False
    from management.services.ig_reply_language import own_reply_text
    text = own_reply_text(response.reply_text)
    pending = re.search(r"(?:ще|поки|пока|ещ[её])[^.!?\n]{0,35}(?:уточн\w*|підтвердж\w*|подтвержд\w*)|"
        r"менеджер\w*[^.!?\n]{0,35}уточн\w*|pending|unconfirmed|not\s+(?:yet\s+)?confirmed", text, re.I)
    return bool(pending and re.search(_DATE, text, re.I) and any(
        re.search(r"критич\w*|важлив\w*|важн\w*|critical|important|без\s+гарант\w*|without[^?]{0,20}guarantee|uncertain", clause, re.I)
        and re.search(_DATE, clause, re.I) for clause in re.findall(r"(?:[^.!?\n]|\.(?=\d))+\?", text)))
