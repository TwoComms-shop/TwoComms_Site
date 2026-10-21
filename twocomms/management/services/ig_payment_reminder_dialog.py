"""Source-bound time clarification, before a separate native PR1 agreement.

A pending request is neither a reminder task nor consent. Its exact delivered
question may authorize interpretation of one subsequent pure time reply only.
"""
from datetime import timedelta
from copy import deepcopy
from uuid import UUID

from django.utils import timezone

from management.ig_payment_reminder_dialog_models import IgPaymentReminderTimeRequest as TimeRequest
from management.services.ig_payment_reminder_time import parse_reminder_time_reply

VERSION = "payment-reminder-time.v1"
LIFETIME = timedelta(minutes=30)


def _reminders():
    from management.services import ig_payment_reminders
    return ig_payment_reminders


def _columns(row):
    return {"id": str(row.pk), "client_id": row.client_id,
        "request_source_id": row.request_source_id, "revision_id": row.revision_id,
        "expires_at": row.expires_at.isoformat(), "window_deadline": row.window_deadline.isoformat(),
        "locale": row.locale, "body": row.body}


def _verified(row):
    reminder = _reminders()
    return bool(row and reminder._verified(row, "time_request")
        and row.envelope.get("version") == VERSION
        and row.envelope.get("columns") == _columns(row)
        and row.locale in {"uk", "ru", "en"})


def _scope_binding(scope, client, *, read_only=False, current_source_cart=None):
    reminder = _reminders()
    deal, proposal, episode, generation, attempt, _ = scope
    cart = reminder._current_cart(proposal, client, read_only=read_only, current_source_cart=current_source_cart)
    if not cart:
        raise ValueError("time_request_cart_unavailable")
    return {"deal_id": deal.pk, "proposal_id": proposal.pk, "episode_id": episode.pk,
        "invoice_generation_id": generation.pk, "attempt_id": attempt.pk,
        "invoice": reminder._fingerprint(proposal, generation, attempt),
        "source_cart": reminder._stable_cart_proof(cart)}


def capture_request(owned, scope, settings_obj, *, locale, now, deadline):
    """Caller holds the client/checkout locks; persist before asking the time."""
    reminder = _reminders()
    revision, source, proof = owned
    if source.provider_created_at > now + timedelta(minutes=1) or deadline <= now:
        raise ValueError("time_request_expired")
    existing = TimeRequest.objects.filter(request_source_id=source.pk).first()
    if existing:
        reason = _request_reason(existing, settings_obj, now=now, scope=scope)
        if reason or existing.revision_id != revision.pk:
            raise ValueError(reason or "time_request_source_changed")
        return existing
    row = TimeRequest(client=revision.client, request_source=source, revision=revision,
        expires_at=min(now + LIFETIME, deadline), window_deadline=deadline,
        locale=locale, body=reminder._copy(locale, "clarify"))
    row.envelope = {"version": VERSION, "columns": _columns(row), "source": proof,
        "scope": _scope_binding(scope, revision.client), "recipient": revision.client.igsid,
        "settings_id": settings_obj.pk, "settings_epoch": settings_obj.reply_permission_epoch,
        "client_epoch": revision.client.reply_permission_epoch,
        "locale_proof": {"source_id": source.pk, "source_digest": proof["digest"],
            "locale": locale, "basis": "explicit_payment_request_language"}}
    row.envelope_hmac = reminder._sign("time_request", row.envelope)
    row.save(force_insert=True)
    return row


def _request_reason(row, settings_obj, *, now, scope=None, check_expiry=True,
    read_only=False, current_source_cart=None):
    reminder = _reminders()
    if not _verified(row):
        return "time_request_signature_invalid"
    owned, reason = reminder._owned_source(row.revision, row.request_source, settings_obj, require_head=False)
    if not owned or owned[2] != row.envelope.get("source"):
        return reason or "time_request_source_changed"
    source = owned[1]
    if (source.quick_reply_payload or source.status == "ignored" or source.attachment_media
        or source.attachments not in (None, "", "[]")):
        return "time_request_source_changed"
    client = owned[0].client
    if (row.client_id != client.pk or row.envelope.get("recipient") != client.igsid
        or row.envelope.get("settings_id") != settings_obj.pk
        or row.envelope.get("settings_epoch") != settings_obj.reply_permission_epoch
        or row.envelope.get("client_epoch") != client.reply_permission_epoch):
        return "time_request_permission_changed"
    if check_expiry and (now >= row.expires_at or now >= row.window_deadline):
        return "time_request_expired"
    if scope is not None:
        try:
            binding = _scope_binding(scope, client, read_only=read_only, current_source_cart=current_source_cart)
        except ValueError:
            return "time_request_purchase_changed"
        if binding != row.envelope.get("scope"):
            return "time_request_purchase_changed"
    return ""


def _prompt_effect(row, *, sent=True):
    """No history/MID guesses; one exact owned native outbox effect."""
    from management.models import IgRevisionDeliveryEffect
    from management.services.ig_delivery_receipts import normalize_provider_message_id
    reminder = _reminders()
    effects = list(IgRevisionDeliveryEffect.objects.filter(revision_id=row.revision_id).order_by("pk")[:2])
    if len(effects) != 1:
        return None
    effect = effects[0]
    proof, envelope = row.envelope["source"], row.envelope
    if (effect.source_message_id != row.request_source_id or effect.actor != "bot"
        or effect.purpose != "normal_reply" or effect.part_count != 1 or effect.part_index != 0
        or effect.recipient_igsid != envelope["recipient"] or effect.provider_namespace != proof["namespace"]
        or effect.settings_id_snapshot != envelope["settings_id"]
        or effect.settings_permission_epoch != envelope["settings_epoch"]
        or effect.client_permission_epoch != envelope["client_epoch"]
        or effect.revision_snapshot_digest != proof["revision_digest"]
        or reminder._digest(effect.payload) != effect.payload_digest or not effect.plan_digest
        or effect.payload.get("recipient") != {"id": envelope["recipient"]}
        or effect.payload.get("message") != {"text": row.body}):
        return None
    if sent:
        if (effect.state != "sent" or not normalize_provider_message_id(effect.provider_message_id)
            or not effect.terminal_at or effect.terminal_at >= row.expires_at
            or effect.terminal_at < row.request_source.provider_created_at):
            return None
    elif effect.state not in {"planned", "claimed", "provider_started"}:
        return None
    return effect


def _prompt_proof(effect):
    return {"effect_id": effect.pk, "payload_digest": effect.payload_digest,
        "plan_digest": effect.plan_digest, "provider_message_id": effect.provider_message_id,
        "delivered_at": effect.terminal_at.isoformat()}


def _intervening_source(row, source):
    from management.models import InstagramBotMessage
    return InstagramBotMessage.objects.filter(client_id=row.client_id, role="user",
        pk__gt=row.request_source_id, pk__lt=source.pk).exists()


def resolve_continuation(owned, scope, settings_obj, *, now):
    """Return a pending intent only after proving its delivered exact question."""
    reminder = _reminders()
    revision, source, _proof = owned
    row = TimeRequest.objects.select_for_update().select_related("client", "request_source", "revision").filter(
        client_id=source.client_id, request_source_id__lt=source.pk).order_by("-request_source_id").first()
    if not row:
        return None, None, "time_request_absent"
    parsed = parse_reminder_time_reply(source.text, source.provider_created_at, locale=row.locale)
    if not parsed.handled:
        return None, None, parsed.reason
    reason = _request_reason(row, settings_obj, now=now, scope=scope)
    if reason:
        return None, None, reason
    effect = _prompt_effect(row)
    if (not effect or effect.terminal_at > now or source.provider_created_at < effect.terminal_at
        or source.provider_created_at >= row.expires_at
        or source.provider_created_at > now + timedelta(minutes=1)
        or (source.reply_to_provider_message_id and source.reply_to_provider_message_id != effect.provider_message_id)
        or _intervening_source(row, source)):
        return None, None, "time_request_prompt_unverified"
    used = reminder.Invitation.objects.filter(envelope__continuation__time_request_id=str(row.pk))
    if used.exclude(revision_id=revision.pk, request_source_id=source.pk).exists():
        return None, None, "time_request_already_used"
    return row, parsed, ""


def continuation_proof(row, source_proof):
    effect = _prompt_effect(row)
    if not effect:
        raise ValueError("time_request_prompt_unverified")
    return {"version": VERSION, "time_request_id": str(row.pk), "time_request_hmac": row.envelope_hmac,
        "prompt": _prompt_proof(effect), "time_source": source_proof}


def invitation_reason(inv, settings_obj):
    """Optional provenance leaves every historical direct PR1 row unchanged."""
    if "continuation" not in inv.envelope:
        return ""
    reminder = _reminders()
    continuation = inv.envelope["continuation"]
    if (not isinstance(continuation, dict) or set(continuation) != {
        "version", "time_request_id", "time_request_hmac", "prompt", "time_source"}
        or continuation.get("version") != VERSION):
        return "time_request_continuation_invalid"
    try:
        identity = UUID(continuation["time_request_id"])
    except (ValueError, TypeError, AttributeError):
        return "time_request_continuation_invalid"
    row = TimeRequest.objects.select_related("client", "request_source", "revision").filter(pk=identity, client_id=inv.client_id).first()
    if not row:
        return "time_request_missing"
    reason = _request_reason(row, settings_obj, now=timezone.now(), check_expiry=False)
    if reason:
        return reason
    effect = _prompt_effect(row)
    if (not effect or continuation.get("time_request_hmac") != row.envelope_hmac
        or continuation.get("prompt") != _prompt_proof(effect)
        or continuation.get("time_source") != inv.envelope.get("source")):
        return "time_request_prompt_unverified"
    frozen = row.envelope["scope"]
    if (inv.deal_id != frozen["deal_id"] or inv.proposal_id != frozen["proposal_id"]
        or inv.commercial_episode_id != frozen["episode_id"]
        or inv.invoice_generation_id != frozen["invoice_generation_id"] or inv.payment_attempt_id != frozen["attempt_id"]
        or inv.envelope.get("invoice") != frozen["invoice"] or inv.envelope.get("source_cart") != frozen["source_cart"]
        or inv.window_deadline != row.window_deadline or inv.locale != row.locale):
        return "time_request_purchase_changed"
    source = inv.request_source
    parsed = parse_reminder_time_reply(source.text, source.provider_created_at, locale=row.locale)
    if (not parsed.handled or parsed.due_at != inv.due_at or source.pk <= row.request_source_id
        or source.provider_created_at < effect.terminal_at or source.provider_created_at >= row.expires_at
        or inv.created_at >= row.expires_at or _intervening_source(row, source)
        or (source.reply_to_provider_message_id and source.reply_to_provider_message_id != effect.provider_message_id)):
        return "time_request_source_changed"
    if reminder.Invitation.objects.filter(envelope__continuation__time_request_id=str(row.pk)).exclude(pk=inv.pk).exists():
        return "time_request_already_used"
    return ""


def provider_boundary_reason(revision_id, *, now=None):
    """Called from the existing reminder boundary before its physical socket."""
    from management.models import InstagramBotSettings, IgClient, InstagramBotMessage
    reminder = _reminders()
    row = TimeRequest.objects.select_related("client", "request_source", "revision").filter(revision_id=revision_id).first()
    if not row:
        return ""
    now = now or timezone.now()
    settings_obj = InstagramBotSettings.objects.filter(pk=row.envelope.get("settings_id")).first()
    if not settings_obj:
        return "settings_missing"
    if not reminder._owned_source(row.revision, row.request_source, settings_obj, require_head=True)[0]:
        return "time_request_source_changed"
    if InstagramBotMessage.objects.filter(client_id=row.client_id, role="user", pk__gt=row.request_source_id).exists():
        return "time_request_newer_intent"
    client = IgClient.objects.select_for_update().filter(pk=row.client_id).first()
    frozen = row.envelope.get("scope") or {}
    scope, reason = reminder._scope(reminder._graph(frozen.get("deal_id"), frozen.get("proposal_id")),
        client, settings_obj, now) if client else (None, "client_missing")
    if not scope:
        return reason
    reason = _request_reason(row, settings_obj, now=now, scope=scope)
    if reason:
        return reason
    return "" if _prompt_effect(row, sent=False) else "time_request_physical_plan_changed"


def _projection_owner(client, settings_obj):
    """Mutable read ownership; no echo, service or provider dispatch policies."""
    return (tuple(getattr(client, name) for name in ("igsid", "current_commercial_episode_id",
        "reply_permission_epoch", "privacy_erasure_started_at", "hidden_at", "opted_out_at", "opted_in_at",
        "bot_paused", "manager_takeover", "is_blocked", "stage", "primary_objection", "lost_reason")),
        settings_obj.pk, settings_obj.is_enabled, settings_obj.reply_permission_epoch,
        settings_obj.ig_user_id, settings_obj.page_id)


def _projection_source(row, settings_obj, *, now, floor):
    from management.models import IgTurnRevisionSource
    reminder = _reminders()
    if (not _verified(row) or not settings_obj or now >= row.expires_at or now >= row.window_deadline
        or row.envelope.get("recipient") != row.client.igsid
        or row.envelope.get("settings_id") != settings_obj.pk
        or row.envelope.get("settings_epoch") != settings_obj.reply_permission_epoch
        or row.envelope.get("client_epoch") != row.client.reply_permission_epoch):
        return False
    source = row.request_source
    if (source.status == "ignored" or source.quick_reply_payload or source.attachment_media
        or source.attachments not in (None, "", "[]")):
        return False
    frozen = IgTurnRevisionSource.objects.filter(revision_id=row.revision_id, message_id=source.pk).first()
    return not reminder._projection_source_reason(row.revision, source, row.envelope["source"],
        row.client, settings_obj, floor, frozen=frozen)


def _purchase_fence(graph, episode):
    """Detached concrete-column snapshot of the actual loaded purchase rows."""
    if not graph or not episode:
        return None
    deal, proposal, generations, attempts = graph
    rows = (deal, proposal, episode, *generations, *[attempts[key] for key in sorted(attempts)])
    if any(row.get_deferred_fields() for row in rows):
        return None
    return tuple((row._meta.label_lower, tuple((field.attname, deepcopy(getattr(row, field.attname)))
        for field in row._meta.concrete_fields)) for row in rows)


def _payment_read_closed(deal_id):
    from management.models import IgPaymentConfirmationReview, IgPaymentProjection
    review = IgPaymentConfirmationReview.objects.filter(deal_id=deal_id).exclude(
        status__in=["cancelled", "superseded"]).exists()
    payment = IgPaymentProjection.objects.filter(deal_id=deal_id).exclude(
        truth__in=["unverified", "pending"]).exists()
    return review or payment


def _projection_case(row, settings_obj, *, now, current_source_cart=None):
    """One bounded, read-only purchase case; never reuse send admission here."""
    from management.models import IgCommercialEpisode
    from management.services.ig_conversation_routes import conversation_route_reset_floor
    reminder = _reminders()
    floor = ((current_source_cart.get("scope") or {}).get("reset_floor", 0)
        if current_source_cart is not None else conversation_route_reset_floor(row.client_id))
    if not _projection_source(row, settings_obj, now=now, floor=floor):
        return None
    binding = row.envelope["scope"]
    graph = reminder._graph(binding["deal_id"], binding["proposal_id"], read_only=True)
    if not graph:
        return None
    episode = IgCommercialEpisode.objects.filter(pk=binding["episode_id"], client_id=row.client_id).first()
    proposal = graph[1]
    revision = proposal.revisions.filter(revision=proposal.revision).first()
    snapshot = revision.snapshot if revision and isinstance(revision.snapshot, dict) else {}
    items = list(proposal.items.order_by("position", "id").values(
        "product_id", "color_variant_id", "quantity", "size", "fit_code", "option_values"))
    cart = reminder._current_cart(proposal, row.client, read_only=True, current_source_cart=current_source_cart,
        revision_snapshot=snapshot, item_rows=items)
    scope, reason = reminder._projection_purchase_scope(graph, row.client, settings_obj, episode, cart, now,
        generation_id=binding["invoice_generation_id"], attempt_id=binding["attempt_id"], invoice=binding["invoice"],
        source_cart=binding["source_cart"], settings_epoch=row.envelope["settings_epoch"],
        client_epoch=row.envelope["client_epoch"], namespace=row.envelope["source"]["namespace"],
        revision_snapshot=snapshot, item_rows=items)
    if not scope or scope[-1] or _payment_read_closed(binding["deal_id"]):
        return None
    fence = _purchase_fence(graph, episode)
    return (cart, floor, fence) if fence is not None else None


def projection(client_id, *, now, current_source_cart=None):
    """Detached awaiting-time state, only for the latest still-owned question."""
    from management.models import InstagramBotSettings, InstagramBotMessage, IgCommercialEpisode
    reminder = _reminders()
    query = TimeRequest.objects.select_related("client", "revision", "request_source")
    row = query.filter(client_id=client_id).order_by("-request_source_id").first()
    if not row or not _verified(row) or now >= row.expires_at:
        return None
    if (InstagramBotMessage.objects.filter(client_id=client_id, role="user", pk__gt=row.request_source_id).exists()
        or reminder.Invitation.objects.filter(client_id=client_id, request_source_id__gte=row.request_source_id).exists()):
        return None
    settings_obj = InstagramBotSettings.objects.filter(pk=row.envelope.get("settings_id")).first()
    if not settings_obj:
        return None
    case = _projection_case(row, settings_obj, now=now, current_source_cart=current_source_cart)
    if not case:
        return None
    cart, floor, purchase_fence = case
    effect = _prompt_effect(row)
    if not effect or effect.terminal_at > now:
        return None
    cards = list(InstagramBotMessage.objects.filter(client_id=client_id, role="model",
        provider_namespace=row.envelope["source"]["namespace"], provider_message_id=effect.provider_message_id,
        text=row.body).order_by("pk")[:2])
    if len(cards) != 1:
        return None
    binding = row.envelope["scope"]
    source_scope = {"client_id": client_id, "episode_id": binding["episode_id"],
        "deal_id": binding["deal_id"], "proposal_id": binding["proposal_id"],
        "proposal_revision": binding["invoice"]["proposal_revision"],
        "invoice_generation_id": binding["invoice_generation_id"], "generation": binding["invoice"]["generation_number"],
        "attempt_id": binding["attempt_id"], "reset_floor": row.envelope["source"]["reset_floor"],
        "source_namespace": row.envelope["source"]["namespace"], **cart}
    result = {"version": 1, "status": "awaiting_time", "reason": "", "captured_at": now.isoformat(),
        "client_id": client_id, "time_request_id": str(row.pk), "invitation_id": None,
        "due_at": None, "due_at_kyiv": None, "task_id": None, "locale": row.locale,
        "policy": reminder.POLICY, "prompt_receipt_confirmed": True,
        "source_ids": {"request": row.request_source_id, "card": cards[0].pk, "answer": None, "send": None},
        "source_times": {"request": row.request_source.provider_created_at.isoformat(),
            "card": effect.terminal_at.isoformat(), "answer": None, "send": None},
        "source_scope": source_scope, "scope_digest": reminder._digest(source_scope)}
    final = query.filter(pk=row.pk).first()
    final_settings = InstagramBotSettings.objects.filter(pk=settings_obj.pk).first()
    final_effect = _prompt_effect(final) if final and _verified(final) else None
    if (not final or not final_settings or final.envelope_hmac != row.envelope_hmac
        or _projection_owner(row.client, settings_obj) != _projection_owner(final.client, final_settings)
        or not final_effect or _prompt_proof(final_effect) != _prompt_proof(effect)
        or InstagramBotMessage.objects.filter(client_id=client_id, role="user", pk__gt=row.request_source_id).exists()):
        return None
    if current_source_cart is not None:
        # A settlement can occur while the final receipt is being read. Close
        # that race with actual fresh purchase rows and payment/review truth,
        # without invoking any provider-send policy. The issuer separately
        # rechecks the supplied source cart and enclosing owner/payment fence.
        final_graph = reminder._graph(binding["deal_id"], binding["proposal_id"], read_only=True)
        final_episode = IgCommercialEpisode.objects.filter(pk=binding["episode_id"], client_id=client_id).first()
        if (_purchase_fence(final_graph, final_episode) != purchase_fence
            or _payment_read_closed(binding["deal_id"])):
            return None
        if not _projection_source(final, final_settings, now=now, floor=floor):
            return None
    else:
        # Standalone callers have no enclosing capture to supply that fence.
        final_case = _projection_case(final, final_settings, now=now)
        if not final_case or final_case != case:
            return None
    return result
