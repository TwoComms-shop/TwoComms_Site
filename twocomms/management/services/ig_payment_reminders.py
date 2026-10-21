"""One exact, explicitly requested reminder; no marketing or invoice issuance.

Payment validation is committed immediately before each physical request.
Permission serialization remains the existing customer-send barrier. This is
admission linearization, not a transaction across an external provider socket.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
import hashlib
import hmac
import json
import re
import uuid
from copy import deepcopy

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from management.ig_payment_reminder_models import (
    IgPaymentReminderAnswer as Answer, IgPaymentReminderInvitation as Invitation,
    IgPaymentReminderReceipt as Receipt,
)
from management.services.ig_payment_reminder_time import (
    format_kyiv_datetime, parse_requested_reminder, parse_reminder_time_reply, quiet_time_allowed,
)

PAYMENT_REMINDER_PREFIX = PREFIX = "PR1:"
ORIGIN = "agreed_payment_reminder"
POLICY = "reminder_only_no_automatic_reissue"
GRACE = timedelta(minutes=5)


@dataclass(frozen=True)
class ReminderRequest:
    handled: bool
    reply_text: str = ""
    quick_replies: tuple = ()
    invitation_id: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class ReminderBoundary:
    ready: bool
    reason: str = ""

    def __bool__(self):
        return self.ready


def _enabled():
    return bool(getattr(settings, "IG_PAYMENT_REMINDERS_ENABLED", False))


def _digest(value):
    from management.services.ig_turn_revisions import _digest as canonical_digest
    return canonical_digest(value)


def _sign(kind, envelope):
    key = str(getattr(settings, "IG_PAYMENT_REMINDER_HMAC_KEY", "") or settings.SECRET_KEY)
    body = json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hmac.new(key.encode(), b"ig-payment-reminder-v1:" + kind.encode() + b":" + body, hashlib.sha256).hexdigest()


def _verified(row, kind):
    return bool(row and isinstance(row.envelope, dict) and row.envelope_hmac
        and hmac.compare_digest(row.envelope_hmac, _sign(kind, row.envelope)))


def _invitation_columns(inv):
    return {"id": str(inv.pk), "client_id": inv.client_id,
        "request_source_id": inv.request_source_id, "revision_id": inv.revision_id,
        "deal_id": inv.deal_id, "proposal_id": inv.proposal_id,
        "episode_id": inv.commercial_episode_id, "generation_id": inv.invoice_generation_id,
        "attempt_id": inv.payment_attempt_id, "due_at": inv.due_at.isoformat(),
        "expires_at": inv.expires_at.isoformat(), "window_deadline": inv.window_deadline.isoformat(),
        "locale": inv.locale, "body": inv.body, "button_title": inv.button_title,
        "accept_payload": inv.accept_payload, "revoke_payload": inv.revoke_payload}


def _valid_invitation(inv):
    from management.services.ig_payment_reminder_dialog import VERSION
    return (_verified(inv, "invitation") and inv.envelope.get("columns") == _invitation_columns(inv)
        and ("continuation" not in inv.envelope or isinstance(inv.envelope["continuation"], dict)
            and inv.envelope["continuation"].get("version") == VERSION))


def _receipt_columns(row):
    return {"invitation_id": str(row.invitation_id), "effect_id": row.effect_id,
        "provider_message_id": row.provider_message_id, "delivered_at": row.delivered_at.isoformat()}


def _answer_columns(row):
    return {"invitation_id": str(row.invitation_id), "source_id": row.source_id,
        "receipt_id": row.receipt_id, "kind": row.kind, "task_id": row.task_id,
        "answered_at": row.answered_at.isoformat()}


def _payload(invitation_id, action):
    identity = f"{PREFIX}{invitation_id}:{action}"
    return f"{identity}:{_sign('payload', {'identity': identity})}"


def _button(inv):
    return {"content_type": "text", "title": inv.button_title, "payload": inv.accept_payload}


def _quick_reply(inv):
    from management.services.ig_message_templates import QuickReply
    return QuickReply(title=inv.button_title, payload=inv.accept_payload)


def _revoke_quick_reply(inv):
    from management.services.ig_message_templates import QuickReply
    return QuickReply(title={"uk": "Скасувати", "ru": "Отменить", "en": "Cancel reminder"}[inv.locale], payload=inv.revoke_payload)


def _revoke_button(inv):
    reply = _revoke_quick_reply(inv)
    return {"content_type": "text", "title": reply.title, "payload": reply.payload}


def _copy(locale, key, value=""):
    copies = {
        "uk": {"clarify": "Коли саме нагадати про оплату? Напишіть дату й час за Києвом, наприклад «сьогодні о 15:30». Нагадування можна запланувати з 10:00 до 21:30, поки діє ця пропозиція. Після вибору часу попросимо окремо підтвердити одне нагадування кнопкою 💛",
            "time_retry": "Цей час не підходить для поточної пропозиції. Напишіть новий повний запит, наприклад «Нагадай про оплату сьогодні о 15:30». Час не переносимо автоматично; нагадування ще не заплановано.",
            "invite": "🕒 Нагадаю про оплату {value} за київським часом.\n\nПідтвердьте одне нагадування кнопкою нижче 💛 Якщо до цього часу рахунок спливе, повідомлю про це без автоматичного створення нового.",
            "button": "Так, нагадайте 💛", "accepted": "Домовились 💛 Одне нагадування про оплату заплановано на {value} за київським часом.",
            "revoked": "Нагадування скасовано 💛", "stale": "Це підтвердження вже не актуальне. Якщо нагадування ще потрібне, напишіть нову дату й час за Києвом.",
            "active": "💛 Як і домовились, нагадую про оплату. Чинне посилання на ваш рахунок:\n{value}",
            "expired": "💛 Як і домовились, нагадую про оплату. Попередній рахунок уже сплив, тому посилання не додаю. Якщо хочете продовжити оформлення, напишіть нам — перевіримо актуальні умови."},
        "ru": {"clarify": "Когда именно напомнить об оплате? Напишите дату и время по Киеву, например «сегодня в 15:30». Напоминание можно запланировать с 10:00 до 21:30, пока действует это предложение. После выбора времени попросим отдельно подтвердить одно напоминание кнопкой 💛",
            "time_retry": "Это время не подходит для текущего предложения. Напишите новый полный запрос, например «Напомни об оплате сегодня в 15:30». Время автоматически не переносим; напоминание ещё не запланировано.",
            "invite": "🕒 Напомню об оплате {value} по киевскому времени.\n\nПодтвердите одно напоминание кнопкой ниже 💛 Если к этому времени счёт истечёт, сообщу об этом без автоматического создания нового.",
            "button": "Да, напомните 💛", "accepted": "Договорились 💛 Одно напоминание об оплате запланировано на {value} по киевскому времени.",
            "revoked": "Напоминание отменено 💛", "stale": "Это подтверждение уже не актуально. Если напоминание ещё нужно, напишите новую дату и время по Киеву.",
            "active": "💛 Как и договорились, напоминаю об оплате. Действующая ссылка на ваш счёт:\n{value}",
            "expired": "💛 Как и договорились, напоминаю об оплате. Предыдущий счёт уже истёк, поэтому ссылку не добавляю. Если хотите продолжить оформление, напишите нам — проверим актуальные условия."},
        "en": {"clarify": "When exactly should I remind you about payment? Send a date and time in Kyiv, for example “today at 15:30”. A reminder can be scheduled between 10:00 and 21:30 while this offer is valid. After choosing the time, we’ll ask you to confirm one reminder with a separate button 💛",
            "time_retry": "That time is not available for this offer. Send a new full request, for example “Remind me to pay today at 15:30”. We do not move the time automatically; no reminder is scheduled yet.",
            "invite": "🕒 I’ll remind you about payment on {value}, Kyiv time.\n\nConfirm one reminder using the button below 💛 If the invoice has expired by then, I’ll let you know without automatically creating another one.",
            "button": "Yes, remind me 💛", "accepted": "Agreed 💛 One payment reminder is scheduled for {value}, Kyiv time.",
            "revoked": "Your reminder is cancelled 💛", "stale": "This confirmation is no longer current. If you still need a reminder, please send a new date and time in Kyiv.",
            "active": "💛 As agreed, here is your payment reminder. Your current invoice link:\n{value}",
            "expired": "💛 As agreed, here is your payment reminder. The previous invoice has expired, so I haven’t included its link. If you’d like to continue, message us and we’ll check the current terms."},
    }
    return copies[locale if locale in copies else "uk"][key].format(value=value)


def _owned_source(revision, source, settings_obj, *, require_head=True):
    from management.models import IgCustomerTurnRevision, InstagramBotMessage
    from management.services.ig_turn_revisions import _source_payload
    from management.services.ig_conversation_routes import conversation_route_reset_floor
    from management.services.instagram_bot import ingress_provider_namespace
    revision = IgCustomerTurnRevision.objects.filter(pk=revision.pk).first()
    if not revision or not revision.snapshot_digest or _digest(revision.bundle_snapshot) != revision.snapshot_digest:
        return None, "revision_unsealed"
    if require_head and (revision.active_slot != 1 or revision.state not in {"sealed", "claimed", "processed"} or revision.origin != "inbound"):
        return None, "revision_not_current"
    row = InstagramBotMessage.objects.filter(pk=getattr(source, "message_id", None) or source.pk).first()
    if not row or row.role != "user" or row.source != "webhook" or row.status == "failed" or row.client_id != revision.client_id:
        return None, "source_not_owned"
    client = revision.client
    floor = conversation_route_reset_floor(client.pk)
    frozen = revision.sources.filter(message_id=row.pk).first()
    snapshot = next((item for item in revision.bundle_snapshot.get("sources", ()) if item.get("message_id") == row.pk), None)
    if (not frozen or not snapshot or row.pk < floor or row.sender_id != client.igsid
        or not row.mid or not row.provider_created_at or row.provider_created_at.tzinfo is None
        or row.provider_namespace != ingress_provider_namespace(settings_obj)
        or row.provider_namespace != frozen.source_namespace
        or snapshot.get("source_digest") != frozen.source_digest
        or _source_payload(row, previous=frozen, ordinal=frozen.ordinal)["source_digest"] != frozen.source_digest
        or revision.permission_epoch != client.reply_permission_epoch
        or revision.erasure_started_at_snapshot != client.privacy_erasure_started_at):
        return None, "source_changed"
    proof = {"message_id": row.pk, "digest": frozen.source_digest, "revision_id": revision.pk,
        "revision_digest": revision.snapshot_digest, "namespace": row.provider_namespace,
        "mid": row.mid, "provider_created_at": row.provider_created_at.isoformat(), "reset_floor": floor}
    return (revision, row, proof), ""


def _graph(deal_id, proposal_id=None, *, read_only=False):
    """Canonical checkout lock order: Deal, Proposal, generations, attempts."""
    from management.models import IgDeal, IgCheckoutProposal, IgCheckoutInvoiceGeneration
    from orders.models import PaymentAttempt
    locked = lambda model: model.objects.all() if read_only else model.objects.select_for_update()
    deal = locked(IgDeal).filter(pk=deal_id).first()
    if not deal:
        return None
    proposal = locked(IgCheckoutProposal).filter(pk=proposal_id or deal.active_checkout_proposal_id, deal_id=deal.pk).first()
    if not proposal:
        return None
    generations = list(locked(IgCheckoutInvoiceGeneration).filter(proposal_id=proposal.pk).order_by("generation", "pk"))
    attempts = {row.pk: row for row in locked(PaymentAttempt).filter(pk__in=[g.payment_attempt_id for g in generations if g.payment_attempt_id]).order_by("pk")}
    return deal, proposal, generations, attempts


def _fingerprint(proposal, generation, attempt, *, revision_snapshot=None, item_rows=None):
    if item_rows is None:
        item_rows = list(proposal.items.order_by("position", "id").values(
            "product_id", "color_variant_id", "quantity", "size", "fit_code", "option_values"))
    if revision_snapshot is None:
        revision_snapshot = proposal.revisions.get(revision=proposal.revision).snapshot
    return {"proposal_revision": proposal.revision, "items_digest": proposal.items_digest,
        "proposal_expiry": proposal.expires_at.isoformat(), "generation_number": generation.generation,
        "generation_revision": generation.proposal_revision, "series_key": generation.series_key,
        "provider": generation.provider, "provider_invoice_id": generation.provider_invoice_id,
        "invoice_expiry": generation.expires_at.isoformat(), "attempt_invoice_id": attempt.monobank_invoice_id,
        "attempt_expiry": attempt.invoice_expires_at.isoformat() if attempt.invoice_expires_at else "",
        "invoice_url": attempt.invoice_url, "cart_digest": _digest(attempt.cart_snapshot),
        "payment_amount": str(generation.payment_amount), "attempt_amount": str(attempt.payment_amount),
        "proposal_items_digest": _digest(item_rows), "proposal_revision_digest": _digest(revision_snapshot)}


def _authenticated_reminder_button_source(source, client):
    """Only an actual source-owned reply to an already delivered signed card."""
    from management.models import IgTurnRevisionSource, InstagramBotSettings, IgRevisionDeliveryEffect
    payload = str(source.quick_reply_payload or "")
    match = re.fullmatch(r"PR1:([0-9a-f-]{36}):(accept|revoke):([0-9a-f]{64})", payload)
    if not match:
        return False
    try:
        identity = uuid.UUID(match.group(1))
    except ValueError:
        return False
    if str(identity) != match.group(1):
        return False
    inv = Invitation.objects.filter(pk=identity, client_id=client.pk).first()
    action = match.group(2)
    if not inv or not _valid_invitation(inv) or payload != (inv.accept_payload if action == "accept" else inv.revoke_payload):
        return False
    receipt = Receipt.objects.select_related("effect").filter(invitation_id=inv.pk).first()
    if not _valid_receipt(inv, receipt) or source.provider_created_at < receipt.delivered_at or source.provider_created_at >= inv.due_at:
        return False
    settings_obj = InstagramBotSettings.objects.filter(pk=inv.envelope["settings_id"]).first()
    frozen = IgTurnRevisionSource.objects.select_related("revision").filter(message_id=source.pk, revision__snapshot_digest__gt="").order_by("-revision__revision").first()
    if not settings_obj or not frozen or not _owned_source(frozen.revision, source, settings_obj, require_head=False)[0]:
        return False
    if action == "accept":
        return bool(source.provider_created_at < inv.expires_at and (not source.reply_to_provider_message_id or source.reply_to_provider_message_id == receipt.provider_message_id))
    accepted = Answer.objects.filter(invitation_id=inv.pk, kind="accept").first()
    if not accepted or not _valid_answer(accepted):
        return False
    proof = _revoke_card(inv, accepted, reply_to=source.reply_to_provider_message_id)
    if not proof:
        return False
    effect = IgRevisionDeliveryEffect.objects.filter(pk=proof["effect_id"]).first()
    return bool(effect and effect.terminal_at and source.provider_created_at >= effect.terminal_at)


def _neutral_cart_continuation(original, fresh, client):
    """Authenticate every extra immutable noop before normalizing its head."""
    from management.models import IgCommerceSelectionTransition, InstagramBotMessage
    from management.services.ig_commerce_projection import _choice_digest, _source_snapshot, _source_fence_row
    from management.services.ig_commerce_state import _request_payload
    from management.services.ig_commerce_types import CommerceTurnRequest
    from management.services.ig_commerce_turns import parse_turn
    from management.services.ig_revision_commerce import INGRESS_SOURCE_PRODUCER
    start, end = original.get("selection_revision"), fresh.get("selection_revision")
    if (type(start) is not int or type(end) is not int or not start < end <= start + 32
        or any(original.get(key) != fresh.get(key) for key in ("scope", "session_id", "generation", "active_index", "active_line_id"))
        or len(original.get("lines", [])) != len(fresh.get("lines", []))):
        return None
    rows = list(IgCommerceSelectionTransition.objects.filter(session_id=original["session_id"],
        to_revision__gte=start, to_revision__lte=end).select_related("session", "source_message__commerce_turn_decision").order_by("to_revision", "pk")[:34])
    if len(rows) != end - start + 1:
        return None
    base, last = rows[0], rows[-1]
    if (base.to_revision != start or base.session.client_id != client.pk
        or base.session.generation != original["generation"] or base.session.commercial_episode_id != original["scope"]["episode_id"]
        or base.session.open_slot != 1 or base.session.state != "open" or base.session.revision != end
        or _choice_digest(base.next_snapshot) != original["fence"]["snapshot_digest"]
        or _choice_digest(last.next_snapshot) != fresh["fence"]["snapshot_digest"]
        or _source_snapshot(base.session.snapshot()) != _source_snapshot(last.next_snapshot)):
        return None
    expected = _source_snapshot(base.next_snapshot)
    new_source_ids = []
    for transition in rows[1:]:
        source = transition.source_message
        decision = getattr(source, "commerce_turn_decision", None)
        if (transition.from_revision != expected.get("revision") or transition.to_revision != transition.from_revision + 1
            or _source_snapshot(transition.previous_snapshot) != expected
            or transition.action != "turn_unresolved" or transition.correction_operation_id
            or transition.reasons != ["no_state_change"] or transition.effects != {}
            or not decision or decision.session_id != transition.session_id or decision.transition_id != transition.pk
            or decision.accepted or decision.is_stale or decision.delivery_required
            or decision.reply_payload != {} or decision.effects_payload != {}
            or source.client_id != client.pk or source.sender_id != client.igsid or source.role != "user"
            or source.source != "webhook" or source.status == "failed" or not source.mid or not source.provider_created_at
            or source.provider_namespace != original["scope"]["source_namespace"]
            or source.pk < original["scope"]["reset_floor"] or source.provider_created_at > timezone.now()
            or source.attachment_media or source.attachments not in {"", "[]"}
            or transition.source_order_key != f"{source.provider_created_at.isoformat()}|{source.mid}"):
            return None
        if source.quick_reply_payload and not _authenticated_reminder_button_source(source, client):
            return None
        text_digest = hashlib.sha256(source.text.encode()).hexdigest()
        binding = {"source_message_id": source.pk, "source_digest": text_digest,
            "product_resolution": "unresolved"}
        recorded = decision.request_payload
        recorded_binding = recorded.get("source_binding") if isinstance(recorded, dict) else None
        # Direct sealed-revision reduction has three canonical keys. Ingress
        # adds its own exact producer marker; neither path may invent fields.
        if recorded_binding == {**binding, "producer": INGRESS_SOURCE_PRODUCER}:
            binding = recorded_binding
        expected_request = _request_payload(CommerceTurnRequest())
        expected_request["source_binding"] = binding
        reparsed = _request_payload(parse_turn(source.text))
        reparsed["source_binding"] = binding
        result = decision.result_payload
        facts = result.get("source_facts") if isinstance(result, dict) else None
        if (recorded != expected_request or reparsed != expected_request or not isinstance(facts, dict)
            or set(result) != {"reason", "candidate_generation", "source_facts"}
            or result.get("candidate_generation") != transition.next_snapshot.get("candidate_generation")
            or result.get("reason") != "no_state_change" or "preference_withdrawal" in result or "line_source_facts" in result
            or facts != {"values": {}, "source_message_id": source.pk, "source_digest": text_digest,
                "episode_id": original["scope"]["episode_id"],
                "line_id": str((transition.next_snapshot.get("lines") or [])[transition.next_snapshot.get("active_index", 0)].get("line_id") or ""),
                "binding": binding}):
            return None
        before, after = _source_snapshot(transition.previous_snapshot), _source_snapshot(transition.next_snapshot)
        for value in (before, after):
            value.pop("revision", None)
            value.pop("last_provider_message_id", None)
        if before != after or transition.next_snapshot.get("last_provider_message_id") != source.mid:
            return None
        expected = _source_snapshot(transition.next_snapshot)
        new_source_ids.append(source.pk)
    original_ids = original["fence"].get("source_ids")
    if (not isinstance(original_ids, list) or not original_ids or len(original_ids) > 64
        or sorted(set(original_ids + new_source_ids)) != fresh["fence"].get("source_ids")):
        return None
    loaded = {row.source_message_id: row.source_message for row in rows}
    missing = set(original_ids) - loaded.keys()
    if missing:
        loaded.update({row.pk: row for row in InstagramBotMessage.objects.filter(pk__in=missing)})
    if any(pk not in loaded or loaded[pk].client_id != client.pk or loaded[pk].sender_id != client.igsid
        or loaded[pk].role != "user" or loaded[pk].source != "webhook" or loaded[pk].status == "failed"
        or loaded[pk].provider_namespace != original["scope"]["source_namespace"] for pk in original_ids):
        return None
    if (_digest({pk: _source_fence_row(loaded[pk]) for pk in original_ids}) != original["fence"]["source_digest"]
        or _digest({pk: _source_fence_row(loaded[pk]) for pk in fresh["fence"]["source_ids"]}) != fresh["fence"]["source_digest"]):
        return None
    normalized = deepcopy(fresh)
    normalized["selection_revision"] = start
    normalized["fence"].update({key: original["fence"][key] for key in ("snapshot_digest", "source_ids", "source_digest")})
    for old_line, new_line in zip(original["lines"], normalized["lines"]):
        old, new = old_line.get("source_selection"), new_line.get("source_selection")
        if (not isinstance(old, dict) or not isinstance(new, dict)
            or old.get("head_transition_id") != base.pk or old.get("head_digest") != original["fence"]["snapshot_digest"]
            or new.get("head_transition_id") != last.pk or new.get("head_digest") != fresh["fence"]["snapshot_digest"]):
            return None
        new["revision"] = old["revision"]
        new["scope"]["revision"] = old["scope"]["revision"]
        new["head_transition_id"] = old["head_transition_id"]
        new["head_digest"] = old["head_digest"]
    return normalized


def _current_cart(proposal, client, *, read_only=False, current_source_cart=None, revision_snapshot=None, item_rows=None):
    """Require the original bound source cart, not a reconstructed UI draft."""
    from management.services.ig_commerce_projection import capture_current_selection_lines
    from management.services.ig_revision_cart_binding import stable_checkout_source_capture, validate_quote_source_positions
    from management.services.ig_revision_checkout import lock_checkout_cart_sources
    if revision_snapshot is None:
        revision = proposal.revisions.filter(revision=proposal.revision).first()
        revision_snapshot = revision.snapshot if revision and isinstance(revision.snapshot, dict) else {}
    snapshot = revision_snapshot
    captured, binding = snapshot.get("source_cart_capture"), snapshot.get("source_cart_binding")
    if not captured or not binding or (not read_only and not lock_checkout_cart_sources(client, captured)):
        return None
    current = current_source_cart if current_source_cart is not None else capture_current_selection_lines(client.pk)
    original = stable_checkout_source_capture(captured)
    fresh = stable_checkout_source_capture(current)
    if original is None or fresh is None:
        return None
    # Reminder/answer sources legitimately advance the capture watermark.
    # They cannot amend any cart/head/source proof: every other field is exact.
    for value in (original, fresh):
        value.pop("source_watermark", None)
        value["fence"].pop("source_watermark", None)
    if original != fresh:
        normalized = _neutral_cart_continuation(original, fresh, client)
        if normalized != original:
            return None
    if item_rows is None:
        item_rows = list(proposal.items.order_by("position", "id").values(
            "product_id", "color_variant_id", "quantity", "size", "fit_code", "option_values"))
    specs = [{"product_id": item["product_id"], "color_variant_id": item["color_variant_id"],
        "qty": item["quantity"], "size": item["size"], "fit_option_code": item["fit_code"],
        "option_values": item["option_values"] or {}} for item in item_rows]
    proof = validate_quote_source_positions(binding=binding, client_id=client.pk, item_specs=specs)
    if not proof.ok:
        return None
    return {"selection_session_id": captured["session_id"], "selection_generation": captured["generation"],
        "selection_revision": current["selection_revision"], "original_selection_revision": captured["selection_revision"],
        "original_source_cart_capture_digest": captured["capture_digest"],
        "binding_digest": _digest(binding), "source_cart_capture_digest": current["capture_digest"]}


def _stable_cart_proof(cart):
    return {key: value for key, value in cart.items() if key not in {"source_cart_capture_digest", "selection_revision"}} if cart else None


def _scope(graph, client, settings_obj, now, *, invitation=None, allow_expired=False, read_only=False, current_source_cart=None):
    from management.models import IgCommercialEpisode, IgCheckoutProposal, IgPaymentProjection, IgPaymentConfirmationReview
    from management.services.ig_reply_boundary import capture_reply_permission
    from management.services.bot_followups import _has_open_service_conversation
    from management.services.instagram_bot import provider_token_configured, ingress_provider_namespace
    permission = capture_reply_permission(settings_obj.pk, client.pk)
    if not _enabled() or not permission:
        return None, permission.reason or "disabled"
    if (client.stage in {"spam", "lead_manager"} or client.primary_objection == "no_buy"
        or client.lost_reason in {"no_buy", "stop"}):
        return None, "customer_stopped_or_manager_routed"
    if not provider_token_configured(settings_obj):
        return None, "provider_capability_missing"
    if _has_open_service_conversation(client):
        return None, "service_case_open"
    if not graph:
        return None, "current_invoice_missing"
    deal, proposal, generations, attempts = graph
    episode_query = IgCommercialEpisode.objects.all() if read_only else IgCommercialEpisode.objects.select_for_update()
    episode = episode_query.filter(client_id=client.pk, open_slot=1, state="active", deal_id=deal.pk).first()
    if (not episode or client.current_commercial_episode_id != episode.pk
        or episode.intended_order_id or episode.order_attribution_id or episode.primary_payment_review_id
        or proposal.commercial_episode_id != episode.pk or proposal.client_id != client.pk
        or deal.client_id != client.pk or deal.active_checkout_proposal_id != proposal.pk
        or not proposal.assisted_checkout_v2 or proposal.expires_at <= now
        or deal.status not in {"quoted", "awaiting_payment"}
        or deal.order_id or deal.paid_at or deal.paid_amount > 0 or deal.refunded_amount > 0
        or deal.payment_truth not in {"unverified", "pending"}
        or deal.payment_status not in {"unpaid", "pending", "processing", ""}
        or proposal.paid_at or proposal.winner_invoice_generation_id or proposal.superseded_by_id
        or IgPaymentProjection.objects.filter(deal_id=deal.pk).exclude(truth__in=["unverified", "pending"]).exists()
        or IgCheckoutProposal.objects.filter(deal_id=deal.pk, id__gt=proposal.pk).exists()):
        return None, "commerce_scope_changed"
    if IgPaymentConfirmationReview.objects.filter(deal_id=deal.pk).exclude(status__in=["cancelled", "superseded"]).exists():
        return None, "payment_review_present"
    cart = _current_cart(proposal, client, read_only=read_only, current_source_cart=current_source_cart)
    if not cart:
        return None, "source_cart_binding_unavailable"
    generation_id = invitation.invoice_generation_id if invitation else proposal.current_invoice_generation_id
    generation = next((g for g in generations if g.pk == generation_id), None)
    attempt = attempts.get(generation.payment_attempt_id) if generation else None
    if (not generation or not attempt or any(g.generation > generation.generation for g in generations)
        or any(a.order_id or a.checkout_winner_claimed or (a.paid_amount or 0) > 0
            or a.status in {"paid", "prepaid", "refunded", "reversed"} for a in attempts.values())
        or any(g.winner_slot or g.paid_at or g.state not in {"failed", "expired", "cancelled", "invoice_created"} for g in generations)
        or generation.proposal_revision != proposal.revision or generation.winner_slot or generation.paid_at
        or attempt.order_id or attempt.checkout_winner_claimed or (attempt.paid_amount or 0) > 0
        or generation.provider != "monobank" or not generation.provider_invoice_id
        or generation.provider_invoice_id != attempt.monobank_invoice_id
        or not attempt.invoice_url.startswith("https://") or not attempt.invoice_expires_at
        or attempt.invoice_expires_at != generation.expires_at
        or attempt.checkout_series_key != generation.series_key or attempt.checkout_generation != generation.generation):
        return None, "invoice_scope_changed"
    if invitation:
        envelope = invitation.envelope
        if (not _valid_invitation(invitation) or invitation.commercial_episode_id != episode.pk
            or invitation.payment_attempt_id != attempt.pk or envelope.get("invoice") != _fingerprint(proposal, generation, attempt)
            or envelope.get("source_cart") != _stable_cart_proof(cart)
            or envelope.get("settings_id") != settings_obj.pk
            or envelope.get("settings_epoch") != permission.settings_epoch
            or envelope.get("client_epoch") != permission.client_epoch
            or envelope.get("source", {}).get("namespace") != ingress_provider_namespace(settings_obj)):
            return None, "agreement_scope_changed"
    current = (proposal.current_invoice_generation_id == generation.pk and proposal.payment_attempt_id == attempt.pk
        and proposal.status == "invoice_created" and generation.state == "invoice_created"
        and generation.active_slot == 1 and attempt.status == "processing"
        and not generation.review_reason and not attempt.error_reason)
    expired = generation.expires_at <= now
    if current and (not expired or allow_expired):
        return (deal, proposal, episode, generation, attempt, expired), ""
    terminal = (allow_expired and expired and generation.state == "expired"
        and generation.review_reason == "invoice_expired" and generation.terminal_at is not None
        and generation.terminal_at >= generation.expires_at and generation.active_slot is None
        and attempt.status == "expired" and attempt.error_reason == "invoice_expired"
        and proposal.current_invoice_generation_id is None and proposal.payment_attempt_id is None
        and proposal.status in {"ready", "viewed"}
        and generation.events.filter(kind="terminalized", payload__reason="invoice_expired", payload__state="expired").exists()
        and (attempt.event_state or {}).get("provider_terminal", {}).get("source") in {None, "system_expiry"})
    if terminal:
        return (deal, proposal, episode, generation, attempt, True), ""
    return None, "invoice_not_current"


def _projection_scope(graph, client, settings_obj, inv, cart, now, *, revision_snapshot=None, item_rows=None):
    """Finite agreement read; execution alone owns full send-policy guards."""
    return _projection_purchase_scope(graph, client, settings_obj, inv.commercial_episode, cart, now,
        generation_id=inv.invoice_generation_id, attempt_id=inv.payment_attempt_id,
        invoice=inv.envelope.get("invoice"), source_cart=inv.envelope.get("source_cart"),
        settings_epoch=inv.envelope.get("settings_epoch"), client_epoch=inv.envelope.get("client_epoch"),
        namespace=inv.envelope["source"]["namespace"], revision_snapshot=revision_snapshot, item_rows=item_rows)


def _projection_purchase_scope(graph, client, settings_obj, episode, cart, now, *, generation_id,
    attempt_id, invoice, source_cart, settings_epoch, client_epoch, namespace,
    revision_snapshot=None, item_rows=None):
    """Pure purchase read shared by actual signed invitations and time requests.

    The caller loads the real episode and exact signed binding. This grants no
    provider dispatch authority; `_scope` still owns every physical send.
    """
    from management.services.ig_reply_boundary import _client_allowed
    from management.services.instagram_bot import ingress_provider_namespace
    if not graph or not cart or not settings_obj or not episode or not _enabled():
        return None, "source_cart_binding_unavailable"
    allowed, reason = _client_allowed(client)
    if not allowed or not settings_obj.is_enabled:
        return None, reason or "global_reply_paused"
    if client.stage in {"spam", "lead_manager"} or client.primary_objection == "no_buy" or client.lost_reason in {"no_buy", "stop"}:
        return None, "customer_stopped_or_manager_routed"
    deal, proposal, generations, attempts = graph
    generation = next((row for row in generations if row.pk == generation_id), None)
    attempt = attempts.get(attempt_id)
    if (deal.client_id != client.pk or proposal.client_id != client.pk or episode.client_id != client.pk
        or not proposal.assisted_checkout_v2
        or client.current_commercial_episode_id != episode.pk or episode.open_slot != 1 or episode.state != "active"
        or episode.deal_id != deal.pk or episode.intended_order_id or episode.primary_payment_review_id or episode.order_attribution_id
        or deal.active_checkout_proposal_id != proposal.pk or proposal.commercial_episode_id != episode.pk
        or deal.order_id or deal.paid_at or deal.paid_amount > 0 or deal.refunded_amount > 0
        or deal.payment_truth not in {"unverified", "pending"} or deal.status not in {"quoted", "awaiting_payment"}
        or proposal.expires_at <= now or proposal.winner_invoice_generation_id or proposal.paid_at or proposal.superseded_by_id
        or not generation or not attempt or generation.proposal_revision != proposal.revision
        or any(row.generation > generation.generation or row.winner_slot or row.paid_at for row in generations)
        or any(row.order_id or row.checkout_winner_claimed or (row.paid_amount or 0) > 0 for row in attempts.values())
        or invoice != _fingerprint(proposal, generation, attempt, revision_snapshot=revision_snapshot, item_rows=item_rows)
        or source_cart != _stable_cart_proof(cart)
        or settings_epoch != settings_obj.reply_permission_epoch
        or client_epoch != client.reply_permission_epoch
        or namespace != ingress_provider_namespace(settings_obj)):
        return None, "agreement_scope_changed"
    current = (proposal.current_invoice_generation_id == generation.pk and proposal.payment_attempt_id == attempt.pk
        and proposal.status == "invoice_created" and generation.state == "invoice_created" and attempt.status == "processing")
    expired = (generation.expires_at <= now and generation.state == "expired" and generation.review_reason == "invoice_expired"
        and attempt.status == "expired" and attempt.error_reason == "invoice_expired"
        and proposal.current_invoice_generation_id is None and proposal.payment_attempt_id is None and proposal.status in {"ready", "viewed"})
    if not current and not expired:
        return None, "invoice_not_current"
    return (deal, proposal, episode, generation, attempt, generation.expires_at <= now), ""


def _projection_source_reason(revision, row, proof, client, settings_obj, floor, *, frozen=None):
    from management.services.ig_turn_revisions import _source_payload
    from management.services.instagram_bot import ingress_provider_namespace
    if (not revision or not row or row.role != "user" or row.source != "webhook" or row.status == "failed"
        or row.client_id != client.pk or row.sender_id != client.igsid or row.pk < floor
        or not row.mid or not row.provider_created_at or revision.client_id != client.pk
        or revision.snapshot_digest != proof.get("revision_digest") or _digest(revision.bundle_snapshot) != revision.snapshot_digest
        or revision.permission_epoch != client.reply_permission_epoch
        or row.provider_namespace != ingress_provider_namespace(settings_obj)):
        return "source_changed"
    frozen = frozen or revision.sources.filter(message_id=row.pk).first()
    if (not frozen or _source_payload(row, previous=frozen, ordinal=frozen.ordinal)["source_digest"] != proof.get("digest")
        or frozen.source_digest != proof.get("digest") or proof.get("message_id") != row.pk
        or proof.get("reset_floor") != floor or proof.get("namespace") != row.provider_namespace
        or proof.get("mid") != row.mid or proof.get("provider_created_at") != row.provider_created_at.isoformat()):
        return "source_changed"
    return ""


def _invitation_source(inv, settings_obj, *, require_head=False):
    proof, reason = _owned_source(inv.revision, inv.request_source, settings_obj, require_head=require_head)
    if proof and proof[2] != inv.envelope.get("source"):
        return None, "request_source_changed"
    if proof and not reason:
        from management.services.ig_payment_reminder_dialog import invitation_reason
        reason = invitation_reason(inv, settings_obj)
        if reason:
            return None, reason
    return proof, reason


def prepare_request(revision, source, *, settings_obj=None, now=None):
    from management.models import InstagramBotSettings, IgCommercialEpisode, IgClient
    from management.services import ig_payment_reminder_dialog as dialog
    if not _enabled():
        return ReminderRequest(False, reason="disabled")
    settings_obj = settings_obj or InstagramBotSettings.load()
    now = now or timezone.now()
    parsed = parse_requested_reminder(getattr(source, "text", ""), getattr(source, "provider_created_at", None))
    continuation = not parsed.handled
    if continuation and not parse_reminder_time_reply(getattr(source, "text", ""),
        getattr(source, "provider_created_at", None), locale="uk").handled:
        return ReminderRequest(False, reason=parsed.reason)
    with transaction.atomic():
        owned, reason = _owned_source(revision, source, settings_obj)
        if not owned:
            return ReminderRequest(False, reason=reason)
        revision, row, proof = owned
        if (revision.sources.count() != 1 or row.quick_reply_payload or row.status == "ignored"
            or row.attachments not in (None, "", "[]") or row.attachment_media
            or revision.bundle_snapshot.get("sources", [{}])[0].get("media_part_count", 0)):
            return ReminderRequest(False, reason="mixed_request")
        client = IgClient.objects.select_for_update().get(pk=row.client_id)
        episode = IgCommercialEpisode.objects.filter(client_id=client.pk, open_slot=1, state="active").first()
        graph = _graph(episode.deal_id) if episode and episode.deal_id else None
        scope, reason = _scope(graph, client, settings_obj, now)
        if not scope:
            if continuation:
                return ReminderRequest(False, reason=reason)
            return ReminderRequest(True, _copy(parsed.locale, "stale"), reason=reason)
        deal, proposal, episode, generation, attempt, _ = scope
        deadline = min(row.provider_created_at + timedelta(hours=23), proposal.expires_at, proposal.created_at + timedelta(hours=12))
        pending = None
        if continuation:
            pending, parsed, reason = dialog.resolve_continuation(owned, scope, settings_obj, now=now)
            if not pending:
                return ReminderRequest(False, reason=reason)
            deadline = pending.window_deadline
        if not parsed.due_at or parsed.due_at <= now or parsed.due_at >= deadline or row.provider_created_at > now + timedelta(minutes=1):
            if pending:
                return ReminderRequest(True, _copy(parsed.locale, "time_retry"), reason=parsed.reason or "outside_window")
            try:
                pending = dialog.capture_request(owned, scope, settings_obj, locale=parsed.locale, now=now, deadline=deadline)
            except ValueError as error:
                return ReminderRequest(True, _copy(parsed.locale, "stale"), reason=str(error))
            return ReminderRequest(True, pending.body, reason=parsed.reason or "outside_window")
        existing = Invitation.objects.filter(revision_id=revision.pk, request_source_id=row.pk).first()
        if existing:
            if not _valid_invitation(existing) or _invitation_source(existing, settings_obj)[1]:
                return ReminderRequest(True, _copy(parsed.locale, "stale"), reason="invitation_signature_invalid")
            return ReminderRequest(True, existing.body, (_quick_reply(existing),), str(existing.pk))
        identity = uuid.uuid4()
        inv = Invitation(id=identity, client=client, request_source=row, revision=revision,
            deal=deal, proposal=proposal, commercial_episode=episode, invoice_generation=generation,
            payment_attempt=attempt, due_at=parsed.due_at, expires_at=min(parsed.due_at, now + timedelta(hours=1)),
            window_deadline=deadline, locale=parsed.locale,
            body=_copy(parsed.locale, "invite", format_kyiv_datetime(parsed.due_at)),
            button_title=_copy(parsed.locale, "button"), accept_payload=_payload(identity, "accept"), revoke_payload=_payload(identity, "revoke"))
        inv.envelope = {"version": 1, "policy": POLICY, "columns": _invitation_columns(inv),
            "source": proof, "invoice": _fingerprint(proposal, generation, attempt),
            "source_cart": _stable_cart_proof(_current_cart(proposal, client)),
            "settings_id": settings_obj.pk, "settings_epoch": settings_obj.reply_permission_epoch,
            "client_epoch": client.reply_permission_epoch,
            "locale_proof": {"source_id": row.pk, "source_digest": proof["digest"], "locale": parsed.locale, "basis": "explicit_payment_request_language"}}
        if pending:
            inv.envelope["continuation"] = dialog.continuation_proof(pending, proof)
            inv.envelope["locale_proof"] = pending.envelope["locale_proof"]
        inv.envelope_hmac = _sign("invitation", inv.envelope)
        inv.save(force_insert=True)
        return ReminderRequest(True, inv.body, (_quick_reply(inv),), str(inv.pk))


@contextmanager
def invitation_provider_boundary(invitation_id, *, now=None):
    from management.models import InstagramBotSettings, IgClient, IgRevisionDeliveryEffect
    now = now or timezone.now()
    with transaction.atomic():
        inv = Invitation.objects.filter(pk=invitation_id).first()
        reason = "invitation_missing"
        if inv and _valid_invitation(inv):
            settings_obj = InstagramBotSettings.objects.filter(pk=inv.envelope.get("settings_id")).first()
            client = IgClient.objects.select_for_update().filter(pk=inv.client_id).first()
            if settings_obj and client:
                _, reason = _invitation_source(inv, settings_obj, require_head=True)
                if not reason and (now >= inv.expires_at or now >= inv.due_at):
                    reason = "invitation_expired"
                if not reason:
                    _, reason = _scope(_graph(inv.deal_id, inv.proposal_id), client, settings_obj, now, invitation=inv)
                if not reason:
                    effects = list(IgRevisionDeliveryEffect.objects.filter(revision_id=inv.revision_id))
                    if len(effects) != 1 or not _valid_native_effect(inv, effects[0], require_sent=False):
                        reason = "invitation_physical_plan_changed"
        elif inv:
            reason = "invitation_signature_invalid"
    yield ReminderBoundary(not reason, reason)


@contextmanager
def provider_boundary_for_revision(revision_id, *, now=None):
    inv = Invitation.objects.filter(revision_id=revision_id).first()
    if inv:
        with invitation_provider_boundary(inv.pk, now=now) as allowed:
            yield allowed
    else:
        from management.services.ig_payment_reminder_dialog import provider_boundary_reason
        with transaction.atomic():
            reason = provider_boundary_reason(revision_id, now=now)
        yield ReminderBoundary(not reason, reason)


def _valid_native_effect(inv, effect, *, require_sent=True):
    from management.services.ig_delivery_receipts import normalize_provider_message_id
    return bool(effect and (effect.state in {"planned", "claimed", "provider_started"} if not require_sent else effect.state == "sent" and normalize_provider_message_id(effect.provider_message_id))
        and effect.revision_id == inv.revision_id and effect.source_message_id == inv.request_source_id
        and effect.actor == "bot" and effect.purpose == "normal_reply"
        and effect.part_count == 1 and effect.part_index == 0
        and effect.recipient_igsid == inv.client.igsid
        and effect.provider_namespace == inv.envelope["source"]["namespace"]
        and effect.settings_id_snapshot == inv.envelope["settings_id"]
        and effect.settings_permission_epoch == inv.envelope["settings_epoch"]
        and effect.client_permission_epoch == inv.envelope["client_epoch"]
        and effect.revision_snapshot_digest == inv.envelope["source"]["revision_digest"]
        and _digest(effect.payload) == effect.payload_digest
        and effect.payload.get("recipient") == {"id": inv.client.igsid}
        and effect.payload.get("message") == {"text": inv.body, "quick_replies": [_button(inv)]})


def _valid_receipt(inv, receipt):
    return bool(_valid_invitation(inv) and _verified(receipt, "receipt")
        and receipt.envelope.get("columns") == _receipt_columns(receipt)
        and receipt.envelope.get("invitation_hmac") == inv.envelope_hmac
        and _valid_native_effect(inv, receipt.effect)
        and receipt.effect.provider_message_id == receipt.provider_message_id
        and receipt.envelope.get("effect_payload_digest") == receipt.effect.payload_digest
        and receipt.envelope.get("effect_plan_digest") == receipt.effect.plan_digest)


def confirm_invitation_delivery(revision_id, *, now=None):
    """Seal evidence only for the exact already-SENT native outbox card."""
    from management.models import IgRevisionDeliveryEffect, InstagramBotSettings
    now = now or timezone.now()
    with transaction.atomic():
        inv = Invitation.objects.select_for_update().filter(revision_id=revision_id).first()
        if not inv or not _valid_invitation(inv):
            return False
        settings_obj = InstagramBotSettings.objects.filter(pk=inv.envelope["settings_id"]).first()
        if not settings_obj or _invitation_source(inv, settings_obj)[1]:
            return False
        existing = Receipt.objects.filter(invitation_id=inv.pk).first()
        if existing:
            return _valid_receipt(inv, existing)
        effects = list(IgRevisionDeliveryEffect.objects.filter(revision_id=revision_id))
        matches = [effect for effect in effects if _valid_native_effect(inv, effect)]
        if len(matches) != 1 or len(effects) != 1:
            return False
        effect = matches[0]
        delivered_at = effect.terminal_at or now
        if delivered_at >= inv.expires_at or delivered_at >= inv.due_at:
            return False
        receipt = Receipt(invitation=inv, effect=effect, provider_message_id=effect.provider_message_id, delivered_at=delivered_at)
        receipt.envelope = {"columns": _receipt_columns(receipt), "invitation_hmac": inv.envelope_hmac,
            "effect_payload_digest": effect.payload_digest, "effect_plan_digest": effect.plan_digest}
        receipt.envelope_hmac = _sign("receipt", receipt.envelope)
        receipt.save(force_insert=True)
        return True


record_delivery = confirm_invitation_delivery


def _valid_answer(answer):
    return bool(_verified(answer, "answer") and answer.envelope.get("columns") == _answer_columns(answer)
        and _valid_receipt(answer.invitation, answer.receipt)
        and answer.envelope.get("receipt_hmac") == answer.receipt.envelope_hmac
        and (answer.kind != "revoke" or _valid_revoke_card(answer.invitation, answer.envelope.get("revoke_card"))))


def _revoke_card(inv, accepted, *, reply_to=""):
    from management.models import IgRevisionDeliveryEffect
    from management.services.ig_delivery_receipts import normalize_provider_message_id
    revision_id = accepted.envelope.get("source", {}).get("revision_id")
    matches = []
    for effect in IgRevisionDeliveryEffect.objects.filter(revision_id=revision_id, source_message_id=accepted.source_id, state="sent"):
        if (effect.actor == "bot" and effect.purpose == "normal_reply" and effect.part_count == 1 and effect.part_index == 0
            and normalize_provider_message_id(effect.provider_message_id)
            and effect.provider_namespace == inv.envelope["source"]["namespace"]
            and effect.recipient_igsid == inv.client.igsid and _digest(effect.payload) == effect.payload_digest
            and effect.payload.get("recipient") == {"id": inv.client.igsid}
            and effect.payload.get("message") == {"text": _copy(inv.locale, "accepted", format_kyiv_datetime(inv.due_at)), "quick_replies": [_revoke_button(inv)]}
            and (not reply_to or effect.provider_message_id == reply_to)):
            matches.append(effect)
    if len(matches) != 1:
        return None
    effect = matches[0]
    return {"effect_id": effect.pk, "payload_digest": effect.payload_digest, "provider_message_id": effect.provider_message_id}


def _valid_revoke_card(inv, proof):
    accepted = Answer.objects.filter(invitation_id=inv.pk, kind="accept").first()
    return bool(isinstance(proof, dict) and accepted and _valid_answer(accepted)
        and _revoke_card(inv, accepted, reply_to=proof.get("provider_message_id", "")) == proof)


def _answer_source_reason(answer, settings_obj):
    from management.models import IgCustomerTurnRevision
    if not _valid_answer(answer):
        return "answer_signature_invalid"
    revision = IgCustomerTurnRevision.objects.filter(pk=answer.envelope.get("source", {}).get("revision_id")).first()
    if not revision:
        return "answer_revision_missing"
    owned, reason = _owned_source(revision, answer.source, settings_obj, require_head=False)
    if (not owned or owned[2] != answer.envelope.get("source")
        or owned[1].quick_reply_payload != answer.envelope.get("payload")
        or owned[1].quick_reply_payload != _payload(answer.invitation_id, answer.kind)):
        return reason or "answer_source_changed"
    return ""


def handle_reply(row, *, revision=None, settings_obj=None, now=None):
    from management.models import InstagramBotSettings, IgCustomerTurnRevision, IgFollowUpTask, IgClient
    if not _enabled():
        return ReminderRequest(False, reason="disabled")
    payload = str(getattr(row, "quick_reply_payload", "") or "")
    if not payload.startswith(PREFIX):
        return ReminderRequest(False, reason="not_signed_quick_reply")
    now = now or timezone.now()
    settings_obj = settings_obj or InstagramBotSettings.load()
    match = re.fullmatch(r"PR1:([0-9a-f-]{36}):(accept|revoke):([0-9a-f]{64})", payload)
    if not match:
        return ReminderRequest(True, _copy("uk", "stale"), reason="invalid_payload")
    identity, action, _ = match.groups()
    try:
        if str(uuid.UUID(identity)) != identity:
            raise ValueError("noncanonical invitation UUID")
    except ValueError:
        return ReminderRequest(True, _copy("uk", "stale"), reason="invalid_payload")
    with transaction.atomic():
        inv = Invitation.objects.select_for_update().filter(pk=identity).first()
        locale = inv.locale if inv and inv.locale in {"uk", "ru", "en"} else "uk"
        invalid = ReminderRequest(True, _copy(locale, "stale"), reason="answer_not_current")
        if not inv or not _valid_invitation(inv) or payload != (inv.accept_payload if action == "accept" else inv.revoke_payload):
            return invalid
        if row.client_id != inv.client_id or row.pk <= inv.request_source_id:
            return invalid
        revision = revision or IgCustomerTurnRevision.objects.filter(client_id=inv.client_id, active_slot=1).first()
        if not revision:
            return invalid
        owned, reason = _owned_source(revision, row, settings_obj)
        if not owned or _invitation_source(inv, settings_obj)[1]:
            return invalid
        revision, row, source_proof = owned
        receipt = Receipt.objects.filter(invitation_id=inv.pk).select_related("effect").first()
        if (not _valid_receipt(inv, receipt) or not row.quick_reply_payload or not row.mid
            or row.provider_created_at < receipt.delivered_at or row.provider_created_at > now + timedelta(minutes=1)
            or (action == "accept" and row.reply_to_provider_message_id and row.reply_to_provider_message_id != receipt.provider_message_id)):
            return invalid
        previous = Answer.objects.filter(invitation_id=inv.pk, kind=action).first()
        if previous:
            if not _valid_answer(previous) or _answer_source_reason(previous, settings_obj):
                return invalid
            if action == "accept" and (not previous.task_id or previous.task.status not in {"pending", "processing", "sent"}):
                return invalid
            return ReminderRequest(True, _copy(locale, "accepted" if action == "accept" else "revoked", format_kyiv_datetime(inv.due_at)),
                (_revoke_quick_reply(inv),) if action == "accept" and previous.task.status == "pending" else (), reason="duplicate_answer")
        client = IgClient.objects.select_for_update().get(pk=inv.client_id)
        scope, reason = _scope(_graph(inv.deal_id, inv.proposal_id), client, settings_obj, now, invitation=inv, allow_expired=True)
        if not scope or now >= inv.due_at or (action == "accept" and (now >= inv.expires_at or row.provider_created_at >= inv.expires_at)):
            return invalid
        task = None
        revoke_card = None
        if action == "accept":
            if Answer.objects.filter(invitation_id=inv.pk, kind="revoke").exists():
                return invalid
            task = IgFollowUpTask.objects.create(client_id=inv.client_id, deal_id=inv.deal_id,
                due_at=inv.due_at, kind="payment", reason=ORIGIN, trigger="reactive",
                event_key=f"{ORIGIN}:{inv.pk}", event_occurred_at=row.provider_created_at,
                event_payload={"origin": ORIGIN, "invitation_id": str(inv.pk), "invitation_hmac": inv.envelope_hmac,
                    "source_id": row.pk, "receipt_id": receipt.pk, "policy": POLICY},
                policy_version="payment-reminder-v1", meta_window_deadline=inv.window_deadline)
        else:
            accepted = Answer.objects.filter(invitation_id=inv.pk, kind="accept").first()
            if not accepted or not _valid_answer(accepted) or _answer_source_reason(accepted, settings_obj):
                return invalid
            revoke_card = _revoke_card(inv, accepted, reply_to=row.reply_to_provider_message_id)
            if not revoke_card:
                return invalid
            task = accepted.task
            if not task or task.status != "pending":
                return invalid
            IgFollowUpTask.objects.filter(pk=task.pk, status="pending").update(status="cancelled", skip_reason="customer_revoked")
            # The accepted answer already owns the task O2O relation.
            task = None
        answer = Answer(invitation=inv, source=row, receipt=receipt, kind=action, task=task, answered_at=row.provider_created_at)
        answer.envelope = {"columns": _answer_columns(answer), "source": source_proof, "receipt_hmac": receipt.envelope_hmac, "payload": payload}
        if revoke_card:
            answer.envelope["revoke_card"] = revoke_card
        answer.envelope_hmac = _sign("answer", answer.envelope)
        answer.save(force_insert=True)
        if action == "accept":
            # Replacement happens only after a newer card has actually been
            # delivered and its signed positive answer is committed here.
            older = Answer.objects.filter(invitation__client_id=inv.client_id, kind="accept",
                task__client_id=inv.client_id, task__reason=ORIGIN,
                task__status="pending").exclude(invitation_id=inv.pk).select_related(
                    "invitation", "invitation__client", "receipt", "receipt__effect", "task")
            for prior in older:
                if _valid_answer(prior) and prior.task_id:
                    IgFollowUpTask.objects.filter(pk=prior.task_id, client_id=inv.client_id,
                        reason=ORIGIN, status="pending").update(status="cancelled", skip_reason="new_reminder_confirmed")
        return ReminderRequest(True, _copy(locale, "accepted" if action == "accept" else "revoked", format_kyiv_datetime(inv.due_at)),
            (_revoke_quick_reply(inv),) if action == "accept" else (), invitation_id=str(inv.pk))


def _task_guard(task, settings_obj, now):
    from management.models import InstagramBotMessage
    if (task is None or task.kind != "payment" or task.reason != ORIGIN or task.discount_percent
        or (task.event_payload or {}).get("origin") != ORIGIN):
        return None, "task_purpose_changed"
    answer = Answer.objects.filter(task_id=task.pk, kind="accept").select_related("invitation", "receipt", "source").first()
    if not answer or not _valid_answer(answer):
        return None, "agreement_signature_invalid"
    inv = answer.invitation
    if InstagramBotMessage.objects.filter(client_id=inv.client_id, sender_id=task.client.igsid,
        role="user", source__in=["webhook", "poll"], provider_namespace=inv.envelope["source"]["namespace"],
        pk__gt=answer.source_id, status__in=["pending", "processing"]).exists():
        return None, "pending_inbound"
    if (task.due_at != inv.due_at or task.meta_window_deadline != inv.window_deadline
        or task.client_id != inv.client_id or task.deal_id != inv.deal_id
        or task.event_key != f"{ORIGIN}:{inv.pk}" or task.event_payload.get("invitation_hmac") != inv.envelope_hmac
        or task.event_payload.get("invitation_id") != str(inv.pk) or task.event_payload.get("source_id") != answer.source_id
        or task.event_payload.get("receipt_id") != answer.receipt_id or task.event_payload.get("policy") != POLICY):
        return None, "task_agreement_changed"
    if now < inv.due_at or now > inv.due_at + GRACE or now >= inv.window_deadline or not quiet_time_allowed(now):
        return None, "exact_time_expired"
    if Answer.objects.filter(invitation_id=inv.pk, kind="revoke").exists():
        return None, "customer_revoked"
    if _invitation_source(inv, settings_obj)[1]:
        return None, "request_source_changed"
    if reason := _answer_source_reason(answer, settings_obj):
        return None, reason
    scope, reason = _scope(_graph(inv.deal_id, inv.proposal_id), task.client, settings_obj, now, invitation=inv, allow_expired=True)
    if not scope:
        return None, reason
    return (inv, scope), ""


def _reminder_text(guard):
    inv, scope = guard
    return _copy(inv.locale, "expired" if scope[-1] else "active", "" if scope[-1] else scope[4].invoice_url)


@contextmanager
def _reminder_provider_boundary(task_id, token, settings_obj, text, *, now=None, physical_admission=None, **delivery):
    from management.models import IgFollowUpTask
    with transaction.atomic():
        candidate = IgFollowUpTask.objects.select_related("client").filter(pk=task_id).first()
        guard, reason = _task_guard(candidate, settings_obj, now or timezone.now()) if candidate else (None, "task_missing")
        task = IgFollowUpTask.objects.select_for_update().filter(pk=task_id).first()
        if not reason and (task.status != "processing" or task.claim_token != token or task.provider_message_id
            or not task.claim_until or task.claim_until <= (now or timezone.now())):
            reason = "claim_changed"
        if not reason and (text != _reminder_text(guard) or task.message_text != text
            or delivery.get("delivered_chunk_count", 0) or delivery.get("planned_chunk_count", 1) != 1):
            reason = "physical_message_changed"
        if reason and task and task.status == "processing" and task.claim_token == token and not task.provider_message_id:
            task.status = "skipped"
            task.skip_reason = reason
            task.claim_token = ""
            task.claim_until = None
            task.save(update_fields=["status", "skip_reason", "claim_token", "claim_until", "updated_at"])
    if physical_admission is not None and not reason:
        physical_admission["admitted"] = True
    yield ReminderBoundary(not reason, reason)


def _not_dispatched_receipt(reason):
    from management.services.instagram_bot import ProviderDeliveryReceipt
    return ProviderDeliveryReceipt(ok=False, kind="cancelled", hint="physical request was not admitted",
        planned_chunk_count=1, delivered_chunk_count=0, failure_boundary=f"preflight:{reason}")


def _finalize(task_id, token, delivery, text, now):
    from management.models import IgFollowUpTask
    from management.services.ig_delivery_receipts import normalize_provider_message_ids
    from management.services.instagram_bot import ProviderDeliveryReceipt
    mids = normalize_provider_message_ids(getattr(delivery, "provider_message_ids", ()))
    native = isinstance(delivery, ProviderDeliveryReceipt)
    ok = native and delivery.ok and len(mids) == 1 and delivery.planned_chunk_count == 1 and delivery.delivered_chunk_count == 1
    with transaction.atomic():
        task = IgFollowUpTask.objects.select_for_update().get(pk=task_id)
        if task.provider_message_id:
            # The native callback may already have durably checkpointed this
            # exact single-part response. No stale object can erase its MID.
            if mids and mids[0] != task.provider_message_id:
                task.status = "ambiguous"
                task.last_error = "native_receipt_identity_changed"
                task.save(update_fields=["status", "last_error", "updated_at"])
                return False
            checkpointed = task.status == "sent"
            if checkpointed:
                _finish_projection(task_id, now=now)
            return checkpointed
        if task.claim_token != token:
            # Even a concurrently cancelled task must retain native evidence.
            if mids:
                task.provider_message_id = mids[0]
                task.status = "sent" if ok else "ambiguous"
                if ok:
                    task.sent_at = task.sent_at or now
                task.save(update_fields=["provider_message_id", "status", "sent_at", "updated_at"])
            return bool(ok)
        if mids:
            task.provider_message_id = mids[0]
        task.status = "sent" if ok else ("skipped" if native and delivery.kind == "cancelled" and not mids else "ambiguous")
        task.skip_reason = "" if ok else str(getattr(delivery, "failure_boundary", "") or "provider_outcome_unknown")[:255]
        task.claim_token = ""
        task.claim_until = None
        task.sent_at = now if ok else None
        task.last_error = "" if ok else str(getattr(delivery, "kind", "unknown"))[:500]
        task.save(update_fields=["status", "provider_message_id", "skip_reason", "claim_token", "claim_until", "sent_at", "last_error", "updated_at"])
    if ok:
        _finish_projection(task_id, now=now)
    return bool(ok)


def _checkpoint_native_mid(task_id, token, mid, text, *, now=None):
    """Actual Send API callback checkpoints its admitted one-part native MID."""
    from management.models import IgFollowUpTask
    from management.services.ig_delivery_receipts import normalize_provider_message_id
    mid = normalize_provider_message_id(mid)
    if not mid:
        return False
    with transaction.atomic():
        task = IgFollowUpTask.objects.select_for_update().get(pk=task_id)
        if task.provider_message_id:
            return task.provider_message_id == mid
        if (task.claim_token != token or task.attempt_count != 1 or task.reason != ORIGIN
            or task.message_text != text or (task.event_payload or {}).get("origin") != ORIGIN):
            return False
        task.provider_message_id = mid
        task.status = "sent"
        task.sent_at = now or timezone.now()
        task.claim_token = ""
        task.claim_until = None
        task.last_error = ""
        task.save(update_fields=["provider_message_id", "status", "sent_at", "claim_token", "claim_until", "last_error", "updated_at"])
    return True


def _finish_projection(task_id, *, now=None):
    """Recover local history after the independent MID commit; never send."""
    from management.models import IgFollowUpTask, InstagramBotMessage, IgClient
    try:
        with transaction.atomic():
            task = IgFollowUpTask.objects.select_for_update().get(pk=task_id)
            if task.status != "sent" or not task.provider_message_id or task.sent_message_id:
                return bool(task.sent_message_id)
            client = IgClient.objects.filter(pk=task.client_id).first()
            if not client or client.privacy_erasure_started_at:
                return False
            answer = Answer.objects.select_related("invitation", "receipt").get(task_id=task.pk)
            if not _valid_answer(answer):
                return False
            task.sent_message = InstagramBotMessage.objects.create(client_id=task.client_id,
                sender_id=client.igsid, role="model", source="followup", status="done",
                text=task.message_text, provider_message_id=task.provider_message_id,
                provider_namespace=answer.invitation.envelope["source"]["namespace"], processed_at=task.sent_at or now or timezone.now())
            task.save(update_fields=["sent_message", "updated_at"])
        return True
    except Exception:
        # Receipt truth was committed before this fallible local projection.
        return False


def process_due_payment_reminders(settings_obj, now=None, limit=50):
    from management.models import IgFollowUpTask, InstagramBotSettings
    from management.services import instagram_bot as bot
    from management.services.ig_reply_boundary import capture_reply_permission, customer_send_boundary
    if not _enabled():
        return 0
    settings_obj = settings_obj or InstagramBotSettings.load()
    clock = now
    now = now or timezone.now()
    for task_id in IgFollowUpTask.objects.filter(reason=ORIGIN, status="sent", sent_message__isnull=True).exclude(provider_message_id="").values_list("pk", flat=True)[:max(0, min(int(limit), 100))]:
        _finish_projection(task_id, now=now)
    # A stranded started attempt is ambiguous forever until existing operator
    # reconciliation proves delivery. It is never requeued or time-shifted.
    IgFollowUpTask.objects.filter(reason=ORIGIN, status="processing", claim_until__lte=now, provider_message_id="").update(status="ambiguous", skip_reason="processing_crash_no_retry", claim_token="", claim_until=None)
    ids = list(IgFollowUpTask.objects.filter(reason=ORIGIN, status="pending", due_at__lte=now).order_by("due_at", "pk").values_list("pk", flat=True)[:max(0, min(int(limit), 100))])
    sent = 0
    for task_id in ids:
        candidate = IgFollowUpTask.objects.filter(pk=task_id).first()
        if not candidate:
            continue
        client, lease = bot.acquire_client_automation_lease(candidate.client_id)
        if not client:
            continue
        token = uuid.uuid4().hex
        try:
            with transaction.atomic():
                candidate = IgFollowUpTask.objects.select_related("client").get(pk=task_id)
                guard, reason = _task_guard(candidate, settings_obj, clock or timezone.now())
                task = IgFollowUpTask.objects.select_for_update().get(pk=task_id)
                if task.status != "pending" or task.provider_message_id or task.attempt_count:
                    continue
                if reason:
                    task.status = "skipped"
                    task.skip_reason = reason
                    task.save(update_fields=["status", "skip_reason", "updated_at"])
                    continue
                text = _reminder_text(guard)
                task.status = "processing"
                task.claim_token = token
                task.claim_until = now + timedelta(minutes=3)
                task.attempt_count = 1
                task.message_text = text
                task.save(update_fields=["status", "claim_token", "claim_until", "attempt_count", "message_text", "updated_at"])
            permission = capture_reply_permission(settings_obj.pk, client.pk)
            physical_admission = {"admitted": False}
            if not bot.renew_client_automation_lease(client.pk, lease):
                delivery = _not_dispatched_receipt("automation_lease_changed")
            else:
                try:
                    delivery = bot.send_text(settings_obj, client.igsid, text, return_receipt=True,
                        provider_message_callback=lambda mid: _checkpoint_native_mid(task_id, token, mid, text, now=clock),
                        permission_boundary_factory=lambda: customer_send_boundary(settings_obj.pk, client.pk, permission),
                        provider_request_boundary_factory=lambda **state: _reminder_provider_boundary(task_id, token, settings_obj, text, now=clock,
                            physical_admission=physical_admission, **state))
                except Exception as exc:
                    # Token lookup and denied-boundary failures precede our
                    # physical admission. An admitted request stays unknown
                    # unless its independent native MID checkpoint exists.
                    delivery = None if physical_admission["admitted"] else _not_dispatched_receipt(f"not_dispatched_{type(exc).__name__}")
            sent += int(_finalize(task_id, token, delivery, text, clock or timezone.now()))
        finally:
            bot.release_client_automation_lease(client.pk, lease)
    return sent


def payment_reminder_projection(client, *, now=None, current_source_cart=None):
    """One detached safe proof for admin/turn adapters, with no bearer values."""
    from management.models import IgClient, InstagramBotSettings, InstagramBotMessage, IgCustomerTurnRevision, IgCheckoutInvoiceGeneration, IgTurnRevisionSource
    from orders.models import PaymentAttempt
    now = now or timezone.now()
    client_id = client if type(client) is int else client.pk
    empty = {"version": 1, "status": "none", "reason": "", "captured_at": now.isoformat(), "client_id": client_id}
    with transaction.atomic():
        from management.services import ig_payment_reminder_dialog as dialog
        awaiting_time = dialog.projection(client_id, now=now, current_source_cart=current_source_cart)
        if awaiting_time:
            return awaiting_time
        inv = Invitation.objects.select_related("client", "revision", "request_source", "commercial_episode", "deal", "proposal").filter(client_id=client_id).order_by("-created_at").first()
        if not inv:
            return empty
        current = inv.client
        if not current or not _valid_invitation(inv):
            return {**empty, "status": "invalid", "reason": "signature_invalid"}
        settings_obj = InstagramBotSettings.objects.filter(pk=inv.envelope.get("settings_id")).first()
        receipt = Receipt.objects.filter(invitation_id=inv.pk).select_related("effect").first()
        answer = Answer.objects.filter(invitation_id=inv.pk, kind="accept").select_related("task__sent_message", "source").first()
        revoked = Answer.objects.filter(invitation_id=inv.pk, kind="revoke").select_related("source").first()
        for action in (answer, revoked):
            if action:
                action.invitation = inv
                if receipt and action.receipt_id == receipt.pk:
                    action.receipt = receipt
        has_receipt = _valid_receipt(inv, receipt)
        has_answer = bool(answer and _valid_answer(answer))
        if current_source_cart is not None and settings_obj:
            floor = (current_source_cart.get("scope") or {}).get("reset_floor", 0)
            answer_source_reason = revoked_source_reason = ""
            for action in (answer, revoked):
                if action and _valid_answer(action):
                    frozen_source = IgTurnRevisionSource.objects.select_related("revision").filter(
                        revision_id=action.envelope.get("source", {}).get("revision_id"), message_id=action.source_id).first()
                    source_revision = frozen_source.revision if frozen_source else None
                    source_reason = _projection_source_reason(source_revision, action.source, action.envelope.get("source", {}), current, settings_obj, floor, frozen=frozen_source)
                    if action.source.quick_reply_payload != action.envelope.get("payload") or action.envelope.get("payload") != _payload(inv.pk, action.kind):
                        source_reason = "answer_source_changed"
                    if action.kind == "accept":
                        answer_source_reason = source_reason
                    else:
                        revoked_source_reason = source_reason
        else:
            answer_source_reason = _answer_source_reason(answer, settings_obj) if has_answer and settings_obj else ""
            revoked_source_reason = _answer_source_reason(revoked, settings_obj) if revoked and settings_obj else ""
        state = "awaiting_delivery" if not has_receipt else "awaiting_confirmation"
        reason = ""
        if has_answer:
            state = answer.task.status if answer.task_id else "invalid"
        elif now >= inv.expires_at:
            state, reason = "expired", "confirmation_expired"
        if revoked and _valid_answer(revoked):
            state = "cancelled"
        if answer_source_reason or revoked_source_reason:
            return {**empty, "status": "invalid", "reason": answer_source_reason or revoked_source_reason}
        if current_source_cart is not None:
            generations = list(IgCheckoutInvoiceGeneration.objects.filter(proposal_id=inv.proposal_id).order_by("generation", "pk"))
            attempts = {row.pk: row for row in PaymentAttempt.objects.filter(pk__in=[g.payment_attempt_id for g in generations if g.payment_attempt_id]).order_by("pk")}
            graph = (inv.deal, inv.proposal, generations, attempts)
            checkout_revision = inv.proposal.revisions.filter(revision=inv.proposal.revision).first()
            revision_snapshot = checkout_revision.snapshot if checkout_revision and isinstance(checkout_revision.snapshot, dict) else {}
            item_rows = list(inv.proposal.items.order_by("position", "id").values(
                "product_id", "color_variant_id", "quantity", "size", "fit_code", "option_values"))
            cart = _current_cart(inv.proposal, current, read_only=True, current_source_cart=current_source_cart,
                revision_snapshot=revision_snapshot, item_rows=item_rows)
            scope, scope_reason = _projection_scope(graph, current, settings_obj, inv, cart, now,
                revision_snapshot=revision_snapshot, item_rows=item_rows)
            source_reason = _projection_source_reason(inv.revision, inv.request_source, inv.envelope["source"], current, settings_obj,
                (current_source_cart.get("scope") or {}).get("reset_floor", 0)) if settings_obj else "settings_missing"
            if not source_reason and "continuation" in inv.envelope:
                source_reason = dialog.invitation_reason(inv, settings_obj)
            owned = not source_reason
        else:
            graph = _graph(inv.deal_id, inv.proposal_id, read_only=True)
            scope, scope_reason = _scope(graph, current, settings_obj, now, invitation=inv, allow_expired=True, read_only=True) if settings_obj else (None, "settings_missing")
            owned, source_reason = _invitation_source(inv, settings_obj) if settings_obj else (None, "settings_missing")
            cart = _current_cart(graph[1], current, read_only=True) if graph and scope else None
        if not scope or not owned or not cart:
            # Terminal deliveries retain their true delivery status; pending
            # agreements cannot be represented as current actionable consent.
            if state not in {"sent", "ambiguous", "cancelled", "skipped", "expired"}:
                state = "blocked"
            reason = scope_reason or source_reason or "source_cart_binding_unavailable"
        card = InstagramBotMessage.objects.filter(client_id=client_id, role="model",
            provider_namespace=inv.envelope["source"]["namespace"], provider_message_id=receipt.provider_message_id).order_by("pk").first() if has_receipt else None
        task = answer.task if has_answer and answer.task_id else None
        sent_message = task.sent_message if task and task.sent_message_id else None
        source_scope = {"client_id": client_id, "episode_id": inv.commercial_episode_id,
            "deal_id": inv.deal_id, "proposal_id": inv.proposal_id,
            "proposal_revision": inv.envelope["invoice"]["proposal_revision"],
            "invoice_generation_id": inv.invoice_generation_id, "generation": inv.envelope["invoice"]["generation_number"],
            "attempt_id": inv.payment_attempt_id, "reset_floor": inv.envelope["source"]["reset_floor"],
            "source_namespace": inv.envelope["source"]["namespace"],
            **(cart or inv.envelope.get("source_cart") or {})}
        action = revoked if revoked and _valid_answer(revoked) else answer if has_answer else None
        sources = {"request": inv.request_source_id, "card": card.pk if card else None,
            "answer": action.source_id if action else None, "send": sent_message.pk if sent_message else None}
        times = {"request": inv.envelope["source"]["provider_created_at"],
            "card": receipt.delivered_at.isoformat() if has_receipt else None,
            "answer": action.answered_at.isoformat() if action else None,
            "send": task.sent_at.isoformat() if task and task.sent_at else None}
        result = {**empty, "status": state, "reason": reason, "invitation_id": str(inv.pk),
            "due_at": inv.due_at.isoformat(), "due_at_kyiv": format_kyiv_datetime(inv.due_at),
            "locale": inv.locale, "task_id": task.pk if task else None, "policy": POLICY,
            "source_ids": sources, "source_times": times, "source_scope": source_scope}
        result["scope_digest"] = _digest(source_scope)
        # The capture facade already fences each cart source/session. Re-read
        # the enclosing owner and graph after assembling the detached proof.
        if scope and current_source_cart is None:
            final_client = IgClient.objects.filter(pk=client_id).first()
            final_graph = _graph(inv.deal_id, inv.proposal_id, read_only=True)
            final_scope, final_reason = _scope(final_graph, final_client, settings_obj, now,
                invitation=inv, allow_expired=True, read_only=True) if final_client else (None, "client_missing")
            final_cart = _current_cart(final_graph[1], final_client, read_only=True) if final_graph and final_scope else None
            if not final_scope or final_cart != cart or final_scope[-1] != scope[-1]:
                return {**empty, "status": "invalid", "reason": final_reason or "projection_capture_changed"}
        return result
