"""Ordinary source-cart checkout after an explicit physical stock recount.

The caller owns the stock transaction and client lock. Manager availability
does not grant purchase, price, payment, order or provider-send authority.
"""
from copy import deepcopy

from django.db import transaction
from django.utils import timezone

from management.services.ig_fulfillment_stock_sync import StockSyncError


class FulfillmentCheckoutError(StockSyncError):
    """A checkout failure must roll back the caller's stock reconciliation."""


def explicit_later_receipt_option(text):
    """Finite explicit changed receiving instruction; a bare yes grants nothing."""
    import re
    from management.services.ig_fulfillment_requirements import _customer_text
    if not isinstance(text, str) or len(text) > 2000 or re.search(
        r"[?？]|\b(?:не|ні|нет|нельзя|несоглас\w*|незгод\w*|нехоч\w*|непогодж\w*|"
        r"відмов\w*|відхил\w*|отказ\w*|отклон\w*|проти|против|"
        r"if|maybe|might|may|perhaps|possibly|could|not|no|never|cannot|against|oppos\w*|object\w*|reject\w*|"
        r"(?:can|don|doesn|didn|couldn|wouldn|shouldn|won|wasn|weren|isn|aren|haven|hasn|hadn|mustn|needn)[\W_]*t|refus\w*|declin\w*|"
        r"якщо|если|можливо|возможно|мабуть|напевно|наверно\w*|ймовір\w*|вероят\w*|гіпотет\w*|гипотет\w*|"
        r"said|says|told|wrote|asked|suggested|proposed|according|сказ\w*|каже|казав\w*|говор\w*|напис\w*|попрос\w*|просив\w*|предлож\w*|пропону\w*)\b"
        r'|["«»“”‘]|(?<!\w)[\x27’]|[\x27’](?!\w)', text, re.I):
        return None
    text, hypothetical = _customer_text(text)
    if hypothetical or re.search(r"\b(?:international|abroad|overseas|cancel|за\s+кордон|скасу\w*|отмен\w*)\b", text, re.I):
        return None
    affirmative = (re.search(r"(?:^|[.!;\n])\s*(?:(?:please|пожалуйста|будь\s+ласка)[\s,]+)?"
        r"(?:send|ship|deliver|відправте|відправляйте|надішліть|надсилайте|доставте|отправьте|отправляйте|пришлите|доставьте)\b", text, re.I)
        or re.search(r"\b(?:I|we)\s+(?:(?:explicitly|now)\s+)?(?:agree|accept|want|need|will|can|would\s+like)\b", text, re.I)
        or re.search(r"\b(?:я|ми|мы)\s+(?:\w+\s+){0,3}(?:згод\w*|погодж\w*|прийма\w*|принима\w*|соглас\w*|хочу|хочемо|хотим|отримаю|отримаємо|получу|получим|можу|можемо|могу|можем)\b", text, re.I)
        or re.search(r"^\s*(?:згод\w*|погодж\w*|прийма\w*|принима\w*|соглас\w*)\b", text, re.I)
        or re.search(r"\b(?:my\s+(?:boyfriend|friend|contact)|мій\s+(?:хлопець|друг)|мой\s+(?:парень|друг))\s+"
            r"(?:will|can|зможе|отримає|сможет|получит)\b", text, re.I))
    if not affirmative:
        return None
    if not re.search(r"\b(?:later|after\s+(?:I\s+leave|my\s+departure)|пізніше|позже)\b", text, re.I):
        return None
    if not re.search(r"\b(?:send|ship|deliver|receiv\w*|agree|accept|відправ\w*|отрима\w*|надішл\w*|отправ\w*|получ\w*)\b", text, re.I):
        return None
    if (re.search(r"\b(?:boyfriend|friend|contact|хлопц\w*|парн\w*|друг\w*)\b", text, re.I)
        and re.search(r"\b(?:Ukraine|Kharkiv|domestic|Україн\w*|Украин\w*|Харк\w*|Харьк\w*)\b", text, re.I)):
        return "later_domestic_contact"
    if re.search(r"\b(?:Nova\s+Poshta|нова\s+пошта|новая\s+почта)\b", text, re.I):
        return "later_domestic_destination"
    return None


def _matching_deadline_lines(request, decision, current_lines):
    """Exact owned garments/recipients; quantity changes cannot erase a cutoff."""
    outcome = (decision.envelope if decision else {}).get("deadline_review") or {}
    affected = set(outcome.get("line_ids") or [row["line_id"] for row in request.envelope.get("lines") or []])
    previous = [row for row in request.envelope.get("lines") or []
        if row.get("line_id") in affected and row.get("target")]
    def same_item(left, right):
        return (left.get("recipient_id", "self") == right.get("recipient_id", "self")
            and {key: value for key, value in left["target"].items() if key != "quantity"}
            == {key: value for key, value in right["target"].items() if key != "quantity"})
    return [row for row in current_lines if row.get("target") and any(same_item(row, old) for old in previous)]


def _deadline_scope_matches(request, cart):
    previous, scope = request.envelope.get("source_scope") or {}, cart.get("scope") or {}
    return (all(previous.get(key) == scope.get(key) for key in ("client_id", "episode_id", "reset_floor"))
        and previous.get("source_namespace") == scope.get("source_namespace")
        and previous.get("selection_session_id") == cart.get("session_id")
        and previous.get("selection_generation") == cart.get("generation"))


def _deadline_requirement_update_applies(request, source, cart, *, now, affected_line_ids=()):
    """Resolve a sealed withdrawal/change to its actual selected targets.

    A global all-items instruction or a sole selected item is unambiguous.
    Expanded carts otherwise require a captured product/line selector; an
    unrelated B deadline change cannot release A's already reviewed hold.
    """
    import re
    from types import SimpleNamespace
    from management.services import ig_fulfillment_reviews as reviews
    from management.services.ig_fulfillment_requirements import _customer_text
    from management.services.ig_commerce_turns import _line_selector
    if cart.get("status") != "captured" or not cart.get("coverage_complete"):
        return False
    text, hypothetical = _customer_text(source.text)
    if hypothetical or re.search(r"[?？]|\b(?:said|says|told|wrote|according|сказ\w*|каже|казав\w*|говор\w*|напис\w*)\b", source.text, re.I):
        return False
    if re.search(r"\b(?:not|never|don[\W_]*t|не|ні)\W+(?:\w+\W+){0,2}"
            r"(?:cancel|withdraw|remove|change|скасу\w*|отмен\w*|змін\w*|меня\w*)\b", text, re.I):
        return False
    watermark = cart.get("source_watermark") or (cart.get("fence") or {}).get("source_watermark") or {}
    cutoff = watermark.get("message_id")
    if type(cutoff) is not int:
        return False
    lines = reviews._lines(cart, cutoff, now)
    scoped = SimpleNamespace(envelope={"deadline_review": {"line_ids": list(affected_line_ids)}})
    held_ids = {row["line_id"] for row in _matching_deadline_lines(request, scoped, lines)}
    if not held_ids:
        return False
    named = set()
    # Resolve names from the original sealed review, never today's mutable
    # catalogue title. A catalogue rename cannot retarget a past instruction.
    captured_names = {}
    for previous in request.envelope.get("lines") or []:
        product = (previous.get("observation") or {}).get("labels", {}).get("product")
        product_id = (previous.get("target") or {}).get("product_id")
        if isinstance(product, str) and product and type(product_id) is int:
            captured_names.setdefault(product_id, set()).add(product)
    for row in lines:
        product_id = (row.get("target") or {}).get("product_id")
        if (any(re.search(r"(?<!\w)" + re.escape(product) + r"(?!\w)", text, re.I)
                for product in captured_names.get(product_id, ()))
            or (type(product_id) is int and re.search(r"\b(?:product|sku|товар|артикул)\s*[#№:]?\s*"
                + str(product_id) + r"(?!\d)", text, re.I))):
            named.add(row["line_id"])
    indexed = set()
    if (source.pk == cutoff and re.search(
            r"\b(?:item|line|position|позици\w*|позиці\w*|рядок|рядка)\b|(?<!\w)line:\d+", text, re.I)):
        selector, reason = _line_selector(text)
        if reason:
            return False
        if selector.get("target_line_id"):
            indexed = {row["line_id"] for row in lines if row["line_id"] == selector["target_line_id"]}
        elif selector.get("target_line_index") is not None:
            index = selector["target_line_index"]
            indexed = {lines[index]["line_id"]} if 0 <= index < len(lines) else set()
    if named and indexed and named != indexed:
        return False
    selected = named or indexed
    explicit_all = bool(re.search(r"\b(?:all|every|всіх|усіх|всі|усі|всех|все)\b", text, re.I))
    if selected:
        return (len(selected) == 1 or explicit_all) and held_ids <= selected
    # An unrecognized "for B" suffix must not turn "all B deadlines" into
    # a global withdrawal. Ask for a finite target instead of guessing.
    qualified = bool(re.search(r"\b(?:for|для|щодо|каса\w*|стосовно|item|line|позици\w*|позиці\w*)\b", text, re.I))
    return (explicit_all and not qualified) or (len(lines) == 1 and not qualified
        and held_ids == {lines[0]["line_id"]})


def deadline_later_acceptance(request, decision, cart, *, now, _allow_target_subset=False):
    """Read verified later customer source, scoped to this reviewed purchase."""
    from management.models import InstagramBotMessage
    from management.services import ig_fulfillment_reviews as reviews
    from management.services.ig_purpose_sources import owned_source
    outcome = (decision.envelope if decision else {}).get("deadline_review") or {}
    if outcome.get("outcome") != "impossible":
        return None
    watermark = cart.get("source_watermark") or (cart.get("fence") or {}).get("source_watermark") or {}
    cutoff = watermark.get("message_id")
    if type(cutoff) is not int:
        return None
    if reviews._cart_identity(cart) != request.envelope.get("cart_identity"):
        if (not _allow_target_subset or not _deadline_scope_matches(request, cart)
            or not _matching_deadline_lines(request, decision, reviews._lines(cart, cutoff, now))):
            return None
    rows = list(InstagramBotMessage.objects.filter(client_id=request.client_id, role="user", source__in=("webhook", "poll"),
        pk__gt=request.envelope["source_id"], pk__lte=cutoff,
        provider_created_at__gt=decision.created_at, provider_created_at__lte=now).exclude(status="failed").order_by("-pk")[:17])
    if len(rows) > 16:
        return None
    settings_obj = reviews._settings()
    # The newest source must itself own changed receiving instructions. Never
    # resurrect old consent through a later ambiguous question or refusal.
    for row in rows[:1]:
        option = explicit_later_receipt_option(row.text)
        if not option or option not in outcome.get("options", []):
            continue
        if (_allow_target_subset and reviews._cart_identity(cart) != request.envelope.get("cart_identity")
            and not _deadline_requirement_update_applies(request, row, cart, now=now,
                affected_line_ids=outcome.get("line_ids") or ())):
            # "Send it later" is ambiguous after B is added. Require the
            # original exact target, a sealed line selector or explicit ALL.
            continue
        frozen = row.revision_sources.select_related("revision").order_by("-revision_id").first()
        owned, _ = owned_source(frozen.revision, row, settings_obj, require_head=False) if frozen else (None, "")
        if owned:
            return {"option": option, "source": owned[2], "request_digest": request.envelope_hmac,
                "decision_digest": decision.envelope_hmac, "requirement_digest": outcome["requirement_digest"]}
    return None


def deadline_checkout_reason(request, decision, cart, *, now):
    """An unresolved hard cutoff holds only carts containing its reviewed lines."""
    from management.services.ig_fulfillment_requirements import active_hard_requirements
    requirements = active_hard_requirements(request.envelope.get("customer_requirements") or [])
    incomplete = (request.envelope.get("deadline_history") or {}).get("status") == "incomplete"
    if not requirements and not incomplete:
        return ""
    outcome = (decision.envelope if decision else {}).get("deadline_review") or {}
    affected = set(outcome.get("line_ids") or [row["line_id"] for row in request.envelope.get("lines") or []])
    if not affected.intersection(row.get("line_id") for row in cart.get("lines") or []):
        return ""
    if incomplete:
        return "" if _deadline_history_withdrawn(request, cart, now=now) else "checkout_hard_deadline_clarification_required"
    if outcome.get("outcome") == "impossible" and deadline_later_acceptance(request, decision, cart, now=now):
        return ""
    return "checkout_hard_deadline_later_receipt_consent_required" if outcome.get("outcome") == "impossible" else "checkout_hard_deadline_review_required"


def _deadline_history_withdrawn(request, cart, *, now):
    """The newest relevant sealed global instruction resolves unknown history.

    Ordinary chatter does not revoke an explicit withdrawal. A later actual
    deadline requirement cannot be silently skipped to resurrect that release.
    """
    from django.db.models import Q
    from management.models import InstagramBotMessage
    from management.services import ig_fulfillment_reviews as reviews
    from management.services.ig_purpose_sources import owned_source
    from management.services.ig_fulfillment_requirements import extract_requirements, global_deadline_withdrawal, deadline_supersession
    from management.services.ig_ingress_source_proof import is_current_customer_source
    cutoff = (cart.get("source_watermark") or {}).get("message_id")
    if type(cutoff) is not int or not _deadline_scope_matches(request, cart):
        return False
    candidate = Q(pk__in=[])
    for stem in ("leave", "leaving", "depart", "виїжджа", "від'їжджа", "від’їжджа", "уезжа",
            "deadline", "date", "дедлайн", "дат", "термін", "срок", "не потрібно до", "не нужно к"):
        candidate |= Q(text__icontains=stem)
    rows = list(InstagramBotMessage.objects.filter(candidate, client_id=request.client_id, role="user",
        pk__gt=request.envelope["source_id"], pk__lte=cutoff, provider_created_at__lte=now,
        provider_created_at__gte=request.created_at, source__in=("webhook", "poll"),
        provider_namespace=request.envelope.get("source_namespace")).exclude(status="failed").select_related("client").order_by("-pk")[:65])
    if len(rows) > 64:
        return False
    for row in rows:
        if not is_current_customer_source(row, namespace=request.envelope.get("source_namespace")):
            continue
        hint = extract_requirements(row.text, provider_timestamp=row.provider_created_at, now=now)
        if not (hint.get("deadline_hard") or deadline_supersession(hint)):
            continue
        if not global_deadline_withdrawal(hint):
            return False
        frozen = row.revision_sources.select_related("revision").order_by("-revision_id").first()
        owned = owned_source(frozen.revision, row, reviews._settings(), require_head=False)[0] if frozen else None
        return bool(owned and _deadline_requirement_update_applies(request, row, cart, now=now))
    return False


def _latest_fulfillment_deadline_checkout_reason(client, cart, *, now=None):
    """Shared source-cart gate for the ordinary proposal/payment owners."""
    from management.services import ig_fulfillment_reviews as reviews
    now = now or timezone.now()
    projection = reviews.fulfillment_review_projection(client.pk, now=now, current_source_cart=cart)
    current = projection.get("current_request") or {}
    if projection.get("status") not in {"pending", "reviewed"} or not current.get("id"):
        # Adding another item changes the cart identity, but cannot erase a
        # cutoff on the exact original item that is still being charged.
        # Only source-current selection changes may reuse this negative hold.
        if projection.get("reason") != "selection_changed":
            return ""
        request = reviews.Request.objects.select_related("client").filter(client_id=client.pk,
            source__isnull=False, created_at__lte=now).order_by("-source_id", "-created_at", "pk").first()
        if not request or reviews._current_reason(request, reviews._settings(), now=now, current_source_cart=cart) != "selection_changed":
            return ""
        if cart.get("scope") and not _deadline_scope_matches(request, cart):
            return ""
        from management.services.ig_fulfillment_requirements import active_hard_requirements
        if not active_hard_requirements(request.envelope.get("customer_requirements") or []):
            return ""
        decision = reviews.Decision.objects.filter(request=request, created_at__lte=now).order_by("-version").first()
        if (not reviews._verified(decision, "decision")
                or decision.envelope.get("request_digest") != request.envelope_hmac):
            decision = None
        outcome = decision.envelope.get("deadline_review") if decision else None
        watermark = cart.get("source_watermark") or (cart.get("fence") or {}).get("source_watermark") or {}
        cutoff = watermark.get("message_id")
        if type(cutoff) is int:
            current_lines = reviews._lines(cart, cutoff, now)
            if _matching_deadline_lines(request, decision, current_lines):
                if outcome and outcome.get("outcome") == "impossible" and deadline_later_acceptance(
                        request, decision, cart, now=now, _allow_target_subset=True):
                    return ""
                return "checkout_hard_deadline_review_required"
        return ""
    request = reviews.Request.objects.filter(pk=current["id"], client_id=client.pk).first()
    decision_id = (projection.get("decision") or {}).get("id")
    decision = reviews.Decision.objects.filter(pk=decision_id, request=request).first() if decision_id else None
    return deadline_checkout_reason(request, decision, cart, now=now) if request else ""


def _retained_deadline_checkout_reason(client, cart, *, now):
    """A newer unrelated review cannot withdraw an earlier exact-item hold.

    This reuses only negative source requirements. It grants no stock, timing,
    price, payment or order authority to the old or added positions.
    """
    from management.services import ig_fulfillment_reviews as reviews
    from management.services.ig_fulfillment_requirements import active_hard_requirements
    scope = cart.get("scope") or {}
    watermark = cart.get("source_watermark") or (cart.get("fence") or {}).get("source_watermark") or {}
    cutoff = watermark.get("message_id")
    if (scope.get("client_id") != client.pk or type(cutoff) is not int
        or not scope.get("source_namespace") or type(scope.get("reset_floor")) is not int):
        return ""
    settings_obj = reviews._settings()
    if settings_obj is None:
        return ""
    query = reviews.Request.objects.select_related("client").filter(client_id=client.pk,
        source__isnull=False, source_id__lte=cutoff, created_at__lte=now, envelope__scope="cart",
        envelope__source_namespace=scope["source_namespace"],
        envelope__source_scope__episode_id=scope.get("episode_id"),
        envelope__source_scope__reset_floor=scope["reset_floor"],
        envelope__source_scope__selection_session_id=cart.get("session_id"),
        envelope__source_scope__selection_generation=cart.get("generation"),
    ).order_by("-source_id", "-created_at", "pk")
    requests = list(query[:17])
    if len(requests) > 16:
        return "checkout_hard_deadline_review_required"
    current_lines = None
    for request in requests:
        incomplete = (request.envelope.get("deadline_history") or {}).get("status") == "incomplete"
        if not reviews._verified(request, "request") or not (incomplete or active_hard_requirements(
                request.envelope.get("customer_requirements") or [])):
            continue
        decision = reviews.Decision.objects.filter(request=request, created_at__lte=now).order_by("-version").first()
        if (not reviews._verified(decision, "decision")
                or decision.envelope.get("request_digest") != request.envelope_hmac):
            decision = None
        outcome = (decision.envelope if decision else {}).get("deadline_review") or {}
        if outcome:
            try:
                reviews.normalize_deadline_review(outcome, request)
            except ValueError:
                decision, outcome = None, {}
        reason = reviews._current_reason(request, settings_obj, now=now,
            current_source_cart=cart, _deadline_hold=True,
            _deadline_hold_line_ids=outcome.get("line_ids") or ())
        if reason not in {"", "selection_changed", "deadline_continuation_bound"}:
            continue
        if current_lines is None:
            current_lines = reviews._lines(cart, cutoff, now)
        if not _matching_deadline_lines(request, decision, current_lines):
            continue
        if incomplete:
            if _deadline_history_withdrawn(request, cart, now=now):
                continue
            return "checkout_hard_deadline_clarification_required"
        if outcome.get("outcome") == "impossible":
            if reason != "deadline_continuation_bound" and deadline_later_acceptance(
                    request, decision, cart, now=now, _allow_target_subset=True):
                continue
            return "checkout_hard_deadline_later_receipt_consent_required"
        return "checkout_hard_deadline_review_required"
    return ""


def fulfillment_deadline_checkout_reason(client, cart, *, now=None):
    """Fence both the current review and older exact-item hard cutoffs."""
    now = now or timezone.now()
    return (_latest_fulfillment_deadline_checkout_reason(client, cart, now=now)
        or _retained_deadline_checkout_reason(client, cart, now=now))


def _fail(reason):
    raise FulfillmentCheckoutError(reason)


def _purchase_evidence(cart):
    """Accepted reducer evidence for every current recipient line, not intent."""
    from management.services.ig_response_cart_plan import source_line_preferences
    rows = cart.get("lines") or []
    if not rows:
        return None
    result = []
    for row in rows:
        preferences = source_line_preferences(cart, row)
        proof = preferences.get("evidence", {}).get("purchase_requested")
        if preferences.get("values", {}).get("purchase_requested") is not True or not proof:
            return None
        result.append({"line_id": row["line_id"], "recipient_id": row.get("recipient_id", "self"),
            "source": deepcopy(proof)})
    return result


def _complete_available_cart(request, decision, cart):
    requested = request.envelope.get("lines") or []
    decided = decision.envelope.get("lines") or []
    expected = {(row.get("line_id"), row.get("recipient_id", "self")) for row in cart.get("lines", [])}
    if (not expected or len(expected) != len(cart.get("lines", [])) or len(requested) != len(expected)
        or {(row.get("line_id"), row.get("recipient_id", "self")) for row in requested} != expected
        or len(decided) != len(requested) or {row.get("line_id") for row in decided} != {row["line_id"] for row in requested}):
        _fail("checkout_full_cart_coverage_missing")
    targets = {row["line_id"]: row.get("target") for row in requested}
    for row in decided:
        target = targets[row["line_id"]]
        if (not target or row.get("availability") != "available"
            or row.get("supply_basis") not in {"warehouse_checked", "accessible_unrecorded"}
            or type(row.get("checked_quantity")) is not int
            or row["checked_quantity"] < target.get("quantity", 1000)):
            _fail("checkout_full_cart_not_ready")


def _reservation_proof(proposal, now):
    from management.models import IgCheckoutInventoryReservation as Reservation
    items = {item.pk: item for item in proposal.items.select_for_update().order_by("pk")}
    rows = list(Reservation.objects.select_for_update().filter(proposal=proposal,
        invoice_generation__isnull=True, state=Reservation.State.ACTIVE, expires_at__gt=now).order_by("pk"))
    covered, proof = set(), []
    for row in rows:
        line_ids = row.line_ids
        if (not isinstance(line_ids, list) or not line_ids or any(type(value) is not int for value in line_ids)
            or len(set(line_ids)) != len(line_ids) or any(value not in items or value in covered for value in line_ids)
            or row.quantity != sum(items[value].quantity for value in line_ids)
            or row.allocation_source not in {"warehouse", "catalog_variant"}):
            _fail("checkout_reservation_scope_changed")
        covered.update(line_ids)
        proof.append({"id": row.pk, "allocation_key": row.allocation_key, "quantity": row.quantity,
            "line_ids": list(line_ids), "fingerprint": row.reservation_fingerprint,
            "expires_at": row.expires_at.isoformat()})
    if covered != set(items):
        _fail("checkout_finite_reservation_missing")
    return proof


def owned_stock_sync_reservation_ids(request, decision, cart, now):
    """Read-only exact initial-offer holds the already verified sync can reuse.

    The sync caller separately fences the signed review and actual source cart.
    No invoice, paid commitment, different cart or historical hold is excluded.
    Stock quantity is deliberately not a source-ownership proof: the explicit
    recount exists precisely because the numeric count may be wrong.
    """
    from management.models import IgCheckoutProposal, IgCheckoutInventoryReservation as Reservation
    from management.services import ig_fulfillment_reviews as reviews
    from management.services.ig_revision_checkout import checkout_owner_scope
    from management.services.ig_revision_cart_binding import same_checkout_source_capture, validate_cart_binding
    from management.services.ig_inventory import _fingerprint
    if (not reviews._verified(request, "request") or not reviews._verified(decision, "decision")
        or decision.request_id != request.pk or decision.envelope.get("request_digest") != request.envelope_hmac
        or request.envelope.get("scope") != "cart" or reviews._cart_identity(cart) != request.envelope.get("cart_identity")):
        return []
    owner = checkout_owner_scope(request.client)
    proposal_id = ((owner or {}).get("deal") or {}).get("active_checkout_proposal_id")
    if not proposal_id:
        return []
    proposal = IgCheckoutProposal.objects.filter(pk=proposal_id, client_id=request.client_id,
        commercial_episode_id=(owner.get("episode") or {}).get("pk"),
        deal_id=owner["deal"]["pk"]).first()
    if (not proposal or proposal.is_expired or proposal.current_invoice_generation_id
        or proposal.winner_invoice_generation_id or proposal.payment_attempt_id
        or proposal.status not in {IgCheckoutProposal.Status.READY, IgCheckoutProposal.Status.VIEWED}):
        return []
    revision = proposal.revisions.filter(revision=proposal.revision).first()
    snapshot = revision.snapshot if revision else {}
    original_capture = snapshot.get("source_cart_capture") if isinstance(snapshot, dict) else None
    if not isinstance(snapshot, dict) or not same_checkout_source_capture(original_capture, cart):
        return []
    binding = snapshot.get("source_cart_binding")
    # The ordinary offer attaches a deal/proposal and legitimately changes the
    # capture's owner_digest. Its immutable binding still hashes the original
    # capture. Exact current source/cart equality above and the live owned
    # proposal checks permit only that self-attachment; they never retarget an
    # old quote to a new source, recipient, selection or quantity.
    bound = validate_cart_binding(binding=binding, current_capture=original_capture,
        expected_scope=cart.get("scope"))
    if bound.status != "ready":
        return []
    items = list(proposal.items.order_by("pk"))
    quote_map = bound.binding.get("quote_line_map") or []
    if len(items) != len(quote_map) or not items:
        return []
    targets = {row["line_id"]: row.get("target") for row in request.envelope["lines"]}
    for item, mapping in zip(items, quote_map):
        target = targets.get(mapping.get("line_id"))
        if not target or any(target.get(key) != value for key, value in (
            ("product_id", item.product_id), ("color_variant_id", item.color_variant_id),
            ("size", item.size), ("fit_option_code", item.fit_code),
            ("option_values", item.option_values), ("quantity", item.quantity))):
            return []
    by_id, covered, result = {item.pk: item for item in items}, set(), []
    rows = list(Reservation.objects.filter(proposal=proposal, invoice_generation__isnull=True,
        state=Reservation.State.ACTIVE, expires_at__gt=now).order_by("pk"))
    for row in rows:
        ids = row.line_ids
        if (not isinstance(ids, list) or not ids or any(type(value) is not int for value in ids)
            or len(set(ids)) != len(ids) or any(value not in by_id or value in covered for value in ids)
            or row.quantity != sum(by_id[value].quantity for value in ids)
            or row.allocation_source not in {"warehouse", "catalog_variant"}
            or row.reservation_fingerprint != _fingerprint(proposal, row.allocation_key)):
            return []
        covered.update(ids)
        result.append(row.pk)
    return result if covered == set(by_id) else []


def prepare_fulfillment_checkout(request, decision, *, actor, now=None):
    """Return stock_ready or proposal_ready; typed errors abort the outer sync.

    A stock_ready result has no proposal, token or reservation. A proposal_ready
    result references the ordinary manager workspace. Public bearer tokens,
    invoices and customer sends belong to their existing checkout owners.
    """
    from django.contrib.auth import get_user_model
    from management.bot_access import has_all_bot_capabilities, OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION
    from management.models import IgClient, IgCheckoutProposal
    from management.services import ig_fulfillment_reviews as reviews
    from management.services.ig_commerce_projection import capture_current_selection_lines
    from management.services.ig_checkout import CheckoutConfigurationError, create_or_update_proposal, _same_revision_source_cart
    from management.services.ig_checkout_payment import CheckoutPaymentError, _revalidate_frozen_proposal
    from management.services.ig_inventory import InventoryReservationError, reserve_proposal_inventory
    from management.services.ig_revision_checkout import (checkout_authority_control, checkout_owner_scope,
        rebind_checkout_cart_owner, compact_checkout_control, checkout_artifact_control, lock_checkout_cart_sources)
    from product_catalog.models import ProductInventoryPolicy

    if not transaction.get_connection().in_atomic_block:
        _fail("checkout_stock_transaction_required")
    now = now or timezone.now()
    actor = get_user_model().objects.filter(pk=getattr(actor, "pk", None)).first()
    if not actor or not actor.is_active or not has_all_bot_capabilities(actor,
        OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION):
        _fail("actor_forbidden")
    actual = reviews.Request.objects.select_for_update().select_related("client", "revision").filter(pk=getattr(request, "pk", None)).first()
    actual_decision = reviews.Decision.objects.select_for_update().filter(pk=getattr(decision, "pk", None), request=actual).first() if actual else None
    if (not actual or not actual_decision or not reviews._verified(actual, "request")
        or not reviews._verified(actual_decision, "decision")
        or actual.envelope_hmac != getattr(request, "envelope_hmac", None)
        or actual_decision.envelope_hmac != getattr(decision, "envelope_hmac", None)):
        _fail("checkout_review_evidence_invalid")
    request, decision = actual, actual_decision
    if request.envelope.get("scope") != "cart":
        _fail("checkout_current_cart_required")
    latest = reviews.Request.objects.filter(client_id=request.client_id, source__isnull=False,
        created_at__lte=now).order_by("-source_id", "-created_at", "pk").values_list("pk", flat=True).first()
    if latest != request.pk or request.created_at > now:
        _fail("checkout_review_superseded")
    client = IgClient.objects.select_for_update().get(pk=request.client_id)
    request.client = client
    settings_obj = reviews._settings()
    cart = capture_current_selection_lines(client.pk, now=now)
    if not lock_checkout_cart_sources(client, cart):
        _fail("checkout_cart_sources_unavailable")
    reason = reviews._current_reason(request, settings_obj, now=now, current_source_cart=cart)
    if reason:
        _fail(reason)
    reason = reviews._decision_reason(request, decision, now=now, current_source_cart=cart)
    if reason:
        _fail(reason)
    reason = deadline_checkout_reason(request, decision, cart, now=now)
    if reason:
        _fail(reason)
    _complete_available_cart(request, decision, cart)
    purchase = _purchase_evidence(cart)
    common = {"request_id": str(request.pk), "request_digest": request.envelope_hmac,
        "decision_id": str(decision.pk), "decision_digest": decision.envelope_hmac,
        "cart_identity": request.envelope["cart_identity"], "source_scope": deepcopy(request.envelope["source_scope"]),
        "stock_authority": "explicit_absolute_recount", "payment_confirmed": False, "order_created": False}
    if purchase is None:
        return {"ready": True, "status": "stock_ready", "reason": "checkout_purchase_authority_missing",
            "proposal_id": None, "reservation_ids": [], "proof": common,
            "manager_workspace_url": f"https://management.twocomms.shop/bot/?section=clients&client_id={client.pk}"}

    # The canonical source gate above replaces only the legacy text/intent gate.
    # All configuration, independent prices and owner/source quote binding still
    # come from the ordinary backend checkout resolver.
    control, reasons = checkout_authority_control(client, {}, source_cart_capture=cart)
    if reasons:
        _fail(reasons[0])
    before = control["checkout_owner_scope"]
    policies = {row.product_id: row.source for row in ProductInventoryPolicy.objects.filter(
        product_id__in=[item["product_id"] for item in control["items"]])}
    if any(policies.get(item["product_id"]) not in {"warehouse", "catalog_variant"} for item in control["items"]):
        _fail("checkout_finite_inventory_policy_required")
    existing_id = (before.get("deal") or {}).get("active_checkout_proposal_id")
    existing = IgCheckoutProposal.objects.select_for_update().filter(pk=existing_id, client=client).first() if existing_id else None
    pay_type, requested_amount, negotiated_total = "online_full", None, None
    evidence_ids = sorted(set((cart.get("fence") or {}).get("source_ids", [])) | {proof["message_id"] for proof in request.envelope["sources"]})
    if existing:
        if existing.current_invoice_generation_id or existing.winner_invoice_generation_id or existing.payment_attempt_id:
            _fail("checkout_generation_already_started")
        if not _same_revision_source_cart(existing, control["source_cart_binding"], control["source_cart_capture"]):
            _fail("checkout_existing_source_cart_changed")
        # Reuse only the independently priced immutable financial agreement.
        # A new recount never authorizes silently repricing an existing offer,
        # losing a negotiated discount or manufacturing replacement evidence.
        try:
            _revalidate_frozen_proposal(existing)
        except CheckoutPaymentError as exc:
            _fail(exc.code)
        pay_type, requested_amount = existing.pay_type, existing.requested_payment_amount
        negotiated_total = existing.quoted_total if existing.negotiated_discount > 0 else None
        evidence_ids = sorted(set(evidence_ids) | set(existing.revisions.get(revision=existing.revision).evidence_message_ids or []))
    try:
        with transaction.atomic():
            proposal = create_or_update_proposal(client=client, pay_type=pay_type, item_specs=control["items"],
                negotiated_total=negotiated_total, requested_payment_amount=requested_amount, allow_promo=existing.allow_promo if existing else True,
                evidence={"message_ids": evidence_ids}, locale=client.language,
                source_cart_binding=control["source_cart_binding"], source_cart_capture=control["source_cart_capture"],
                checkout_owner_scope=before)
            reserve_proposal_inventory(proposal, require_policy=True)
            reservations = _reservation_proof(proposal, now)
            client.refresh_from_db()
            after = checkout_owner_scope(client)
            owned = rebind_checkout_cart_owner(control, before=before, after=after, checkout=proposal)
            if owned is None:
                _fail("checkout_cart_owner_rebind_failed")
            checkout_revision = proposal.revisions.get(revision=proposal.revision)
            artifact = {"kind": "checkout_revision", "proposal_id": proposal.pk,
                "checkout_revision_id": checkout_revision.pk, "revision": proposal.revision}
            reference = compact_checkout_control(owned, artifact=artifact)["source_cart_reference"]
            persisted, reasons = checkout_artifact_control(client, reference)
            if reasons or persisted.get("items") != owned["items"]:
                _fail("checkout_cart_artifact_rebind_failed")
    except CheckoutConfigurationError as exc:
        _fail(exc.reason or exc.code)
    except InventoryReservationError as exc:
        _fail(exc.reason)
    return {"ready": True, "status": "proposal_ready", "reason": "", "proposal_id": proposal.pk,
        "proposal_public_id": str(proposal.public_id), "proposal_revision": proposal.revision,
        "manager_workspace_url": f"https://management.twocomms.shop/bot/?section=clients&client_id={client.pk}",
        "reservation_ids": [row["id"] for row in reservations],
        "proof": {**common, "purchase_sources": purchase, "owner_before": before, "owner_after": after,
            "source_cart_reference": reference, "quote_digest": checkout_revision.digest,
            "reservations": reservations}}
