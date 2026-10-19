"""Own ordinal ad choices become ordinary source-proved cart operations.

An ad view alone is never a choice. Historical replay uses the immutable
accepted decision's reviewed mapping witness, never today's campaign mapping.
"""
from dataclasses import replace
import hashlib
import re

from management.services.ig_ad_context import capture_ad_context, digest, MAX_CANDIDATES
from management.services.ig_commerce_types import CommerceLineOperation

_ORDINAL = re.compile(r"(?<!\w)(першу|перша|первую|first|другу|друга|вторую|second|третю|третью|third)(?!\w)", re.I)
_POSITIONS = {"першу": 1, "перша": 1, "первую": 1, "first": 1,
    "другу": 2, "друга": 2, "вторую": 2, "second": 2, "третю": 3, "третью": 3, "third": 3}
_BOTH = re.compile(r"\b(?:обидві|обидва|обе|оба|both)\b", re.I)
_VISUAL = re.compile(r"\b(?:справа|праворуч|зліва|ліворуч|on\s+the\s+(?:right|left))\b", re.I)
_AD = re.compile(r"\b(?:реклам\w*|оголошенн\w*|advert\w*|ad|ad_reference)\b", re.I)
_CART = re.compile(r"\b(?:кошик\w*|корзин\w*|cart|рядок|line|позиці\w*)\b", re.I)
_CHOOSE = re.compile(r"\b(?:беру|обираю|вибираю|выбираю|возьму|choose|select|take|додайте|добавьте|add)\b", re.I)


def _own_choice_text(text):
    from management.services.ig_reply_language import own_reply_text
    from management.services.ig_commerce_turns import _current_source_text, _quoted_preference_spans
    if not isinstance(text, str) or len(text) > 64_000:
        return ""
    quoted = list(_quoted_preference_spans(text))
    masked = list(text)
    for span in quoted:
        masked[span.start():span.end()] = " " * (span.end() - span.start())
    clauses = []
    reported_continuation = False
    for span in re.finditer(r"[^;.\n]+(?:[;.\n]|$)", "".join(masked)):
        clause = own_reply_text(text[span.start():span.end()])
        # A customer's explicit reference to the ad is not quoted ad copy.
        # All original bytes remain the source digest; this is parsing only.
        protected = re.sub(r"\b(?:з|із|из|в|у|from)\s+(?:реклам\w*|the\s+advertisement)\b",
            " ad_reference ", clause, flags=re.I)
        reported = re.search(r"\b(?:сказ\w*|говор\w*|каже|кажуть|said|wrote)\b", protected, re.I)
        closed_report = bool(reported and any(quote.start() >= span.start() + reported.start()
            and quote.end() <= span.end() for quote in quoted))
        if reported or reported_continuation:
            offset = reported.end() if reported else 0
            own_correction = re.search(r"\b(?:але\s+я|но\s+я|but\s+i)\b", protected[offset:], re.I)
            protected = protected[offset + own_correction.start():] if own_correction else ""
            reported_continuation = not (closed_report or own_correction)
        clauses.append(_current_source_text(protected))
    return ";".join(clauses)


def _intent(text):
    text = _own_choice_text(text)
    if _ad_catalog_question(text, own=True):
        return True
    if "?" in text or len(text) > 2000:
        return False
    # Recipient wording is not an ordinal selector.
    masked = re.sub(r"\b(?:для|for)\s+(?:мого\s+|моего\s+|my\s+)?\w+", "", text, flags=re.I)
    if _CART.search(masked) or not (_ORDINAL.search(masked) or _BOTH.search(masked) or _VISUAL.search(masked)):
        return False
    from management.services.ig_commerce_turns import _negated_action
    return bool(any(not _negated_action(masked[:match.start()]) for match in _CHOOSE.finditer(masked))
        or re.fullmatch(r"\s*(?:першу|другу|третю|обидві|обидва|обе|оба|first|second|third|both)[.!]?\s*", masked, re.I))


def _ad_catalog_question(text, *, own=False):
    text = text if own else _own_choice_text(text)
    return bool(len(text) <= 500 and text.count("?") == 1 and _AD.search(text)
        and re.search(r"\b(?:що|які|який|what|which|что|какие)\b", text, re.I)
        and re.search(r"річ|речі|товар\w*|виріб|модел\w*|одяг|принт\w*|product|item|design|що\s+є|что\s+есть|what\s+is", text, re.I)
        and not re.search(r"розмір\w*|размер\w*|\bsize\b|крій|крой|\bfit\b|колір\w*|цвет\w*|colou?r|цін\w*|цен\w*|price|оплат\w*|paid|payment|скарг\w*|complaint", text, re.I))


def _witnesses(client, source, revision_id=None):
    from management.models import IgTurnRevisionSource
    from management.services.ig_turn_revisions import _source_payload
    from management.services.ig_conversation_routes import conversation_route_reset_floor
    if (source.client_id != client.pk or source.sender_id != client.igsid or source.role != "user"
        or source.source != "webhook" or source.status == "failed" or not source.mid
        or not source.provider_namespace or client.privacy_erasure_started_at):
        return None
    rows = IgTurnRevisionSource.objects.filter(message_id=source.pk, revision__client_id=client.pk)
    rows = rows.filter(revision_id=revision_id) if revision_id else rows.filter(revision__active_slot=1)
    anchor = rows.select_related("revision").order_by("-pk").first()
    if anchor is None:
        return None
    revision = anchor.revision
    if revision.snapshot_digest and digest(revision.bundle_snapshot) != revision.snapshot_digest:
        return None
    floor = conversation_route_reset_floor(client.pk)
    rows = list(IgTurnRevisionSource.objects.filter(revision=revision, ordinal__lte=anchor.ordinal)
        .select_related("message").order_by("ordinal")[:65])
    if not rows or len(rows) > 64 or source.pk < floor:
        return None
    sources, witnesses = [], []
    for row in rows:
        message = row.message
        if message.pk < floor:
            continue
        if (message.client_id != client.pk or message.sender_id != client.igsid or message.role != "user"
            or message.source != "webhook" or message.status == "failed"
            or message.provider_namespace != source.provider_namespace
            or (message.provider_created_at or message.created_at) > (source.provider_created_at or source.created_at)):
            return None
        payload = _source_payload(message, previous=row, ordinal=row.ordinal)
        if payload["source_digest"] != row.source_digest:
            return None
        if revision.snapshot_digest and not any(item.get("message_id") == message.pk
                and item.get("source_digest") == row.source_digest
                for item in revision.bundle_snapshot.get("sources", [])):
            return None
        sources.append(payload)
        witnesses.append(dict(message_id=message.pk, source_digest=row.source_digest,
            referral_digest=digest(row.referral)))
    return revision.pk, sources, witnesses, floor


def _previous_question_context(client, source, floor):
    """One immediately preceding canonical product question, never last-ad CRM."""
    from management.models import (InstagramBotMessage, IgRevisionDeliveryEffect,
        GeminiRequest, IgCommercialEpisode, IgWebhookInboxEvent)
    previous = InstagramBotMessage.objects.filter(client_id=client.pk, role="user", pk__lt=source.pk,
        created_at__lt=source.created_at).order_by("-created_at", "-pk").first()
    if (previous is None or previous.pk < floor or previous.sender_id != client.igsid
        or previous.provider_namespace != source.provider_namespace or previous.source != "webhook"
        or previous.status == "failed"
        or (previous.provider_created_at or previous.created_at) >= (source.provider_created_at or source.created_at)):
        return None
    episode_id = getattr(client, "current_commercial_episode_id", None)
    if episode_id and not IgCommercialEpisode.objects.filter(pk=episode_id, client_id=client.pk,
            opened_watermark_message_id__lte=previous.pk, open_slot=1).exists():
        return None
    if IgWebhookInboxEvent.objects.filter(namespace=source.provider_namespace, customer_igsid=client.igsid,
            decision="accepted", reason="attribution_only", processed_at__isnull=False,
            received_at__gt=previous.created_at, received_at__lte=source.created_at).exists():
        # A newly observed click is another acquisition context, not an answer
        # to this older bot question. The next reply must clarify its identity.
        return None
    effect = IgRevisionDeliveryEffect.objects.filter(revision__client_id=client.pk, actor="bot", state="sent",
        terminal_at__gt=previous.created_at, terminal_at__lt=source.created_at).select_related("revision").order_by("-terminal_at", "-pk").first()
    if (effect is None or effect.source_message_id != previous.pk or effect.kind != "text"
        or effect.part_count != 1 or effect.provider_namespace != source.provider_namespace
        or effect.recipient_igsid != client.igsid or not effect.provider_message_id
        or effect.client_permission_epoch != getattr(client, "reply_permission_epoch", 0)
        or effect.payload_digest != digest(effect.payload)
        or effect.revision_snapshot_digest != effect.revision.snapshot_digest
        or (source.reply_to_provider_message_id and source.reply_to_provider_message_id != effect.provider_message_id)):
        return None
    # Any intervening manager/holding/other model reply closes this question.
    last_reply = InstagramBotMessage.objects.filter(client_id=client.pk, role="model",
        created_at__gt=previous.created_at, created_at__lt=source.created_at).order_by("-created_at", "-pk").first()
    text = str((effect.payload.get("message") or {}).get("text") or "")
    questions = re.findall(r"[^.!?\n]*\?", text)
    question = questions[0] if len(questions) == 1 else ""
    if (last_reply is None or last_reply.source != "revision_reply" or last_reply.text != text
        or not question
        or not re.search(r"яку|який|яка|які|which|what|какую|какой", question, re.I)
        or not re.search(r"річ|речі|модел\w*|принт\w*|product|item|design", question, re.I)
        or re.search(r"розмір\w*|размер\w*|\bsize\b|крій|крою|крой|\bfit\b|колір\w*|цвет\w*|colou?r", question, re.I)
        or (effect.revision.generation_proposal.get("response") or {}).get("controls")):
        return None
    graph = GeminiRequest.objects.filter(request_id=effect.generation_request_id, client_id=client.pk,
        source_message_id=previous.pk, terminal_resolution="succeeded").first()
    manifest = ((graph.policy_manifest or {}).get("request_context") or {}).get("ad_context_snapshot") if graph else None
    if not isinstance(manifest, dict) or manifest.get("status") != "resolved" or len(manifest.get("touches") or []) != 1:
        return None
    witness = _witnesses(client, previous, effect.revision_id)
    if witness is None:
        return None
    context = capture_ad_context(witness[1], dict(reset_floor=floor, source_namespace=source.provider_namespace))
    from management.services.ig_ad_context import ad_context_manifest
    actual = ad_context_manifest(context)
    # Candidate identities/order/review version must be the original dispatch
    # snapshot. Changed mappings require a new clarification, never reinterpretation.
    if actual.get("status") != manifest.get("status") or actual.get("touches") != manifest.get("touches"):
        return None
    return context, dict(source_message_id=previous.pk, revision_id=effect.revision_id,
        source_witnesses=witness[2], delivery_effect_id=effect.pk, payload_digest=effect.payload_digest,
        generation_request_id=graph.request_id, manifest_digest=digest(manifest))


def _request_from_evidence(source, request, evidence):
    from management.services.ig_commerce_turns import _recipient
    reason = evidence.get("reason", "")
    if _ad_catalog_question(source.text):
        reason = "ambiguous_line_target"
    if any(operation.operation not in {"add", "select"} for operation in request.line_operations):
        reason = "ambiguous_line_operation"
    operations = []
    if not reason:
        by_position = {row["position"]: row["product_id"] for row in evidence["candidates"]}
        clauses = [clause.strip() for clause in re.split(r"[;\n]+", _own_choice_text(source.text)) if clause.strip()]
        for clause in clauses:
            if re.search(r"\b(?:не|ні|not|no|або|или|or|крім|кроме|except|excluding)\b", clause, re.I):
                reason = "ambiguous_line_target"
                break
            masked = re.sub(r"\b(?:для|for)\s+(?:мого\s+|моего\s+|my\s+)?\w+", "", clause, flags=re.I)
            positions = list(dict.fromkeys(_POSITIONS[m.group().casefold()] for m in _ORDINAL.finditer(masked)))
            if _BOTH.search(masked):
                if positions or len(by_position) != 2:
                    reason = "ambiguous_line_target"
                    break
                positions = sorted(by_position)
            recipient, recipient_reason = _recipient(clause)
            if recipient_reason or not positions or any(position not in by_position for position in positions):
                reason = recipient_reason or "ambiguous_line_target"
                break
            # Ad reference establishes identity/recipient only. Size, color,
            # stock, prices and checkout retain their existing independent owners.
            if request.field_updates or request.hard or request.preferences:
                reason = "ambiguous_line_operation"
                break
            operations.extend(CommerceLineOperation("add", exact_product_id=by_position[position],
                recipient_id=recipient or "self") for position in positions)
        if not operations or len(operations) > 8:
            reason = reason or "ambiguous_line_target"
    binding = dict(source_message_id=source.pk, source_digest=hashlib.sha256(source.text.encode()).hexdigest(),
        product_resolution="reviewed_ad_choice" if not reason else "ambiguous_ad_choice", ad_choice=evidence)
    if request.source_binding.get("producer") == "revision-inbound-commerce-v1":
        binding["producer"] = "revision-inbound-commerce-v1"
    return replace(request, exact_product_id=None, exact_unique_alias=False, exact_reference=None,
        field_updates={}, hard={}, preferences={}, recipient_id="", purchase_requested=False,
        checkout_requested=False, full_cart_purchase_requested=False, pending_clarification="",
        line_operations=tuple(operations) if not reason else (),
        pending_line_clarification=reason, source_binding=binding)


def _previous_question_witness_valid(client, source, proof):
    from management.models import IgRevisionDeliveryEffect, GeminiRequest
    if not isinstance(proof, dict):
        return False
    effect = IgRevisionDeliveryEffect.objects.filter(pk=proof.get("delivery_effect_id"),
        revision_id=proof.get("revision_id"), revision__client_id=client.pk, state="sent",
        source_message_id=proof.get("source_message_id"), provider_namespace=source.provider_namespace,
        terminal_at__lt=source.created_at).select_related("source_message").first()
    if (effect is None or effect.payload_digest != proof.get("payload_digest")
        or digest(effect.payload) != effect.payload_digest
        or effect.generation_request_id != proof.get("generation_request_id")):
        return False
    graph = GeminiRequest.objects.filter(request_id=effect.generation_request_id, client_id=client.pk,
        source_message_id=effect.source_message_id, terminal_resolution="succeeded").first()
    manifest = ((graph.policy_manifest or {}).get("request_context") or {}).get("ad_context_snapshot") if graph else None
    witness = _witnesses(client, effect.source_message, effect.revision_id)
    return bool(witness and witness[2] == proof.get("source_witnesses") and digest(manifest) == proof.get("manifest_digest"))


def ad_choice_source_request(client, source, request=None, *, historical=False):
    """Return a canonical ad choice or None when this is another source kind."""
    if not _intent(source.text) or re.search(r"https?://", source.text, re.I):
        return None
    if request is None:
        from management.services.ig_commerce_turns import parse_turn
        request = parse_turn(source.text)
    if request.exact_product_id or any(op.exact_product_id for op in request.line_operations):
        return None
    evidence = None
    if historical:
        from management.models import IgCommerceTurnDecision
        decision = IgCommerceTurnDecision.objects.filter(source_message_id=source.pk).first()
        evidence = ((decision.request_payload or {}).get("source_binding") or {}).get("ad_choice") if decision else None
        if not isinstance(evidence, dict) or evidence.get("schema") != "reviewed-ad-choice.v1":
            return None
        witness = _witnesses(client, source, evidence.get("revision_id"))
        if not witness or evidence.get("sources") != witness[2] or evidence.get("reset_floor") != witness[3]:
            return None
        if evidence.get("digest") != digest({k: v for k, v in evidence.items() if k != "digest"}):
            return None
        if evidence.get("previous_question") and not _previous_question_witness_valid(client, source, evidence["previous_question"]):
            return None
    else:
        witness = _witnesses(client, source)
        if witness is None:
            return None
        revision_id, sources, witnesses, floor = witness
        context = capture_ad_context(sources, dict(reset_floor=floor, source_namespace=source.provider_namespace))
        previous_question = None
        if not context.get("present"):
            prior = _previous_question_context(client, source, floor)
            if prior:
                context, previous_question = prior
        if not context.get("present"):
            return None
        touches = context.get("touches") or []
        reason = ""
        if _ad_catalog_question(source.text):
            reason = "ambiguous_line_target"
        if _VISUAL.search(source.text):
            reason = "ambiguous_line_target"  # List ordinal is not a coordinate.
        if context.get("status") != "resolved" or len(touches) != 1:
            reason = "ambiguous_line_target"
        touch = touches[0] if len(touches) == 1 else {}
        candidates = touch.get("candidates") or []
        review = touch.get("review") or {}
        if (not review.get("reviewed_by") or not review.get("reviewed_at")
            or not candidates or len(candidates) > MAX_CANDIDATES
            or any(not isinstance(row.get("position"), int) or isinstance(row.get("position"), bool) for row in candidates)):
            reason = "ambiguous_line_target"
        from management.models import IgCommerceSelectionSession
        if not previous_question and not _AD.search(_own_choice_text(source.text)) and IgCommerceSelectionSession.objects.filter(
                client_id=client.pk, open_slot=1, lines__isnull=False).exclude(lines=[]).exists():
            reason = "ambiguous_line_target"
        evidence = dict(schema="reviewed-ad-choice.v1", revision_id=revision_id, sources=witnesses,
            reset_floor=floor, context_digest=context.get("context_digest", ""),
            mappings=[dict(mapping_id=t.get("mapping_id"), revision=t.get("revision"), mapping_digest=t.get("mapping_digest")) for t in touches],
            candidates=[dict(product_id=row["product_id"], position=row["position"]) for row in candidates], reason=reason)
        if previous_question:
            evidence["previous_question"] = previous_question
        evidence["digest"] = digest(evidence)
    return _request_from_evidence(source, request, evidence)
