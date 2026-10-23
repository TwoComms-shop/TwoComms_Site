"""Build a bounded, read-only authority snapshot for final IG reply checks.

The model response is never a source of truth here.  A ``price_quoted`` value
is only a candidate for the existing catalog/configuration resolver, and
proposed controls are never copied into ``authorized_actions``.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import hmac
import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from itertools import islice
from typing import Iterable, Mapping
from urllib.parse import quote, urlsplit

from django.conf import settings
from django.utils import timezone

from management.services.ig_core_policy import ORDINARY_DISPATCH_WINDOW_DAYS
from management.services.ig_reply_truth import (AvailabilityAuthority, CheckoutLineAuthority, FulfillmentDeadlineAuthority,
    CheckoutOrderAuthority, ReplyTruthContext, ShippingAuthority, TimingAuthority)


MAX_CATALOG_QUOTES = 8
MAX_SERVER_URLS = 16
MAX_TIMING_CLAIMS = 16
READINESS_GAP_CODES = frozenset({
    "catalog_quote_unverified",
    "checkout_url_not_current",
    "current_configuration_unverified",
    "current_episode_unavailable",
    "current_order_conflict",
    "server_url_invalid",
    "server_url_not_owned",
    "site_origin_unconfigured",
    "current_checkout_order_unverified",
})
EVIDENCE_CODES = frozenset({
    "configured_server_url",
    "current_episode",
    "current_episode_manager_payment",
    "current_episode_order",
    "current_episode_paid_order",
    "current_episode_proposal",
    "current_episode_provider_payment",
    "current_configuration_price",
    "current_order_carrier_delivery",
    "current_order_preparing",
    "current_order_shipped",
    "exact_catalog_quote",
    "explicit_server_timing",
    "source_proved_conversation_amounts",
    "source_bound_fulfillment_pending",
    "manager_reviewed_fulfillment_timing",
    "current_native_checkout_order",
})


@dataclass(frozen=True)
class AuthorityReplyTruthContext(ReplyTruthContext):
    """Validator-compatible context with content-free build diagnostics."""

    readiness_gaps: tuple[str, ...] = ()
    evidence_codes: tuple[str, ...] = ()
    action_authority: str = "external_business_gates"


def _money(value) -> Decimal | None:
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return amount if amount.is_finite() and amount >= 0 else None


CHECKOUT_ORDER_SCHEMA = "current-checkout-order.v1"


def _snapshot_keys():
    values = [settings.SECRET_KEY, *getattr(settings, "SECRET_KEY_FALLBACKS", ())]
    return {hashlib.sha256(value.encode()).hexdigest()[:24]: value for value in values if value}


def _snapshot_mac(body, key_id):
    key = _snapshot_keys().get(key_id)
    if not key:
        return ""
    return hmac.new(key.encode(), (CHECKOUT_ORDER_SCHEMA + ":" + json.dumps(body, ensure_ascii=False,
        sort_keys=True, separators=(",", ":"), allow_nan=False)).encode(), hashlib.sha256).hexdigest()


def _shipping_wire(fact):
    return {key: str(value) if isinstance(value, Decimal) else value for key, value in vars(fact).items()}


def checkout_order_authority(snapshot, *, client_id, episode_id):
    """Pure frozen-source decoder. A raw Order label or unsigned DTO is unknown."""
    try:
        keys = {"schema", "scope", "order_number", "winner_generation_id", "attempt_id", "observed_at",
            "items", "money", "checkout_changes", "shipping_authorities", "source_ref", "key_id", "hmac"}
        if not isinstance(snapshot, dict) or set(snapshot) != keys or snapshot["schema"] != CHECKOUT_ORDER_SCHEMA:
            return None
        body = {key: value for key, value in snapshot.items() if key not in {"key_id", "hmac"}}
        if not isinstance(snapshot["key_id"], str) or not isinstance(snapshot["hmac"], str):
            return None
        expected = _snapshot_mac(body, snapshot["key_id"])
        if not expected or not hmac.compare_digest(expected, snapshot["hmac"]):
            return None
        scope, money = body["scope"], body["money"]
        if (not isinstance(scope, dict) or set(scope) != {"client_id", "episode_id", "order_id"}
            or any(type(value) is not int or value <= 0 for value in scope.values())
            or scope["client_id"] != client_id or scope["episode_id"] != episode_id
            or type(body["attempt_id"]) is not int or body["attempt_id"] <= 0
            or not isinstance(body["order_number"], str) or not body["order_number"]
            or not isinstance(money, dict) or set(money) != {"gross", "discount", "merchandise_payable", "payable", "confirmed_paid", "balance", "currency"}
            or money["currency"] != "UAH"):
            return None
        amounts = {key: _money(money[key]) for key in ("gross", "discount", "merchandise_payable", "payable")}
        if (any(value is None for value in amounts.values()) or amounts["gross"] - amounts["discount"] != amounts["merchandise_payable"]
            or amounts["payable"] < amounts["merchandise_payable"]):
            return None
        paid = _money(money["confirmed_paid"]) if money["confirmed_paid"] is not None else None
        balance = _money(money["balance"]) if money["balance"] is not None else None
        if (money["confirmed_paid"] is not None and paid is None
            or (paid is None) != (balance is None)
            or paid is not None and balance != max(amounts["payable"] - paid, Decimal("0"))):
            return None
        if not isinstance(body["items"], list) or not 1 <= len(body["items"]) <= 40:
            return None
        items = []
        for row in body["items"]:
            if (not isinstance(row, dict) or set(row) != {"line_id", "title", "quantity", "unit_price", "line_total", "size", "fit", "color"}
                or type(row["quantity"]) is not int or not 1 <= row["quantity"] <= 1000
                or any(not isinstance(row[key], str) for key in ("line_id", "title", "size", "fit", "color"))
                or not row["line_id"] or not row["title"]):
                return None
            unit, total = _money(row["unit_price"]), _money(row["line_total"])
            if unit is None or total is None:
                return None
            items.append(CheckoutLineAuthority(row["line_id"], row["title"], row["quantity"], unit, total,
                row["size"], row["fit"], row["color"]))
        if sum((item.line_total for item in items), Decimal("0")) != amounts["gross"]:
            return None
        shipping = []
        for raw in body["shipping_authorities"]:
            raw = dict(raw)
            for key in ("customer_maximum", "customer_charge", "threshold", "basis_total"):
                if raw[key] is not None:
                    raw[key] = _money(raw[key])
                    if raw[key] is None:
                        return None
            shipping.append(ShippingAuthority(**raw))
        if not isinstance(body["checkout_changes"], list) or not isinstance(body["source_ref"], dict):
            return None
        source = body["source_ref"]
        if (set(source) - {"proposal_id", "proposal_revision", "generation_id", "attempt_id", "items_digest", "settlement_status", "amendment_hmacs", "paid_at", "payment_ledger"}
            or any(type(source[key]) is not int or source[key] <= 0 for key in ("proposal_id", "proposal_revision", "attempt_id"))
            or source["attempt_id"] != body["attempt_id"] or source["generation_id"] != body["winner_generation_id"]
            or source["generation_id"] is not None and (type(source["generation_id"]) is not int or source["generation_id"] <= 0)
            or source["settlement_status"] not in {"paid", "prepaid", "converted"}
            or not isinstance(source["items_digest"], str) or not re.fullmatch(r"[a-f0-9]{64}", source["items_digest"])
            or not isinstance(source["amendment_hmacs"], list)
            or datetime.fromisoformat(body["observed_at"]).tzinfo is None):
            return None
        if source.get("paid_at") is not None:
            paid_at = datetime.fromisoformat(source["paid_at"])
            if paid_at.tzinfo is None or paid_at > datetime.fromisoformat(body["observed_at"]):
                return None
        ledger = source.get("payment_ledger")
        if ledger is not None and (not isinstance(ledger, dict)
            or set(ledger) != {"review_id", "decision_id", "projection_id", "reconciliation_state", "needs_reconciliation", "digest"}
            or any(value is not None and (type(value) is not int or value <= 0)
                for value in (ledger["review_id"], ledger["decision_id"], ledger["projection_id"]))
            or type(ledger["needs_reconciliation"]) is not bool
            or not isinstance(ledger["reconciliation_state"], str)
            or not isinstance(ledger["digest"], str) or not re.fullmatch(r"[a-f0-9]{64}", ledger["digest"])):
            return None
        for change in body["checkout_changes"]:
            if (not isinstance(change, dict) or set(change) != {"amendment_id", "source_hmac", "changed_at", "operation", "line_id", "quantity", "before", "after"}
                or change["operation"] not in {"quantity", "remove"}
                or not isinstance(change["source_hmac"], str) or not re.fullmatch(r"[a-f0-9]{64}", change["source_hmac"])
                or not isinstance(change["before"], list) or not isinstance(change["after"], list)
                or datetime.fromisoformat(change["changed_at"]).tzinfo is None):
                return None
        if source["amendment_hmacs"] != [change["source_hmac"] for change in body["checkout_changes"]]:
            return None
        return CheckoutOrderAuthority(scope["client_id"], scope["episode_id"], scope["order_id"], body["order_number"],
            body["winner_generation_id"], body["attempt_id"], body["observed_at"], tuple(items),
            amounts["gross"], amounts["discount"], amounts["merchandise_payable"], amounts["payable"], paid, balance,
            tuple(shipping), json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))
    except (TypeError, ValueError, KeyError, InvalidOperation):
        return None


def checkout_communication_prompt_guidance(context):
    """Mandatory generation input from an already captured context; no reread."""
    fact = getattr(context, "checkout_authority", None)
    if not isinstance(fact, CheckoutOrderAuthority):
        shipping = getattr(context, "shipping_authorities", ())
        if not shipping and "current_episode_proposal" not in getattr(context, "evidence_codes", ()):
            return ""
        return ("[CURRENT CHECKOUT COMMUNICATION]\nExplain that the customer can review the basket and delivery terms "
            "on the checkout link. Mention quantity/removal controls only when the current server state permits editing. "
            "Before payment, explain naturally that removing items or reducing quantity can remove automatic free delivery "
            "when the discounted merchandise total falls below the approved threshold. Read the resulting current checkout "
            "total and payer; never call an old invoice or the original proposal basket the paid basket. After payment, "
            "use only the captured native Order/winning attempt facts for items, amount paid and balance. A deposit is not "
            "full payment. Do not claim any edits occurred without a dated native receipt. "
            "Only these current verified shipping facts may be stated; thresholds are policy amounts, not product prices:\n"
            + json.dumps([_shipping_wire(value) for value in shipping], ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return ("[CURRENT NATIVE CHECKOUT ORDER — VERIFIED DATA]\n" + json.dumps(fact.as_dict(), ensure_ascii=False,
        sort_keys=True, separators=(",", ":")) + "\nUse the actual current Order items, quantities, discounted payable total, "
        "confirmed paid amount and balance in this frozen snapshot. Earlier proposal prices, removed items and chat wishes "
        "are historical context, never the current charged basket. A deposit confirms only the stated paid amount; "
        "do not say fully paid while balance is positive or unknown. source_ref.paid_at is the native payment-confirmation "
        "timestamp when present; observed_at is only the capture clock, never a substitute payment date. "
        "Checkout changes are dated native customer actions. "
        "Automatic free delivery requires the discounted merchandise threshold AND full payment; 200+COD pays delivery separately. An unknown payment choice is only conditional eligibility. The threshold never applies to the deposit; it is not a product price "
        "or a carrier quote. Explain the verified payer naturally and help with the next step. Source IDs, hashes and proofs are backend metadata, never customer prose.")


def _append_unique(values: list, value) -> None:
    if value not in values:
        values.append(value)


def _configured_origin() -> tuple[str, str, int | None] | None:
    raw = str(getattr(settings, "SITE_BASE_URL", "") or "").strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return None
    return parsed.scheme.casefold(), parsed.hostname.casefold(), port


def _catalog_control_urls(control) -> tuple[str, ...]:
    """Resolve link candidates from published products, never from reply prose."""
    if not isinstance(control, Mapping) or not control.get("catalog_link"):
        return ()
    from management.services.ig_catalog_media import parse_product_ids
    from storefront.services.public_products import public_products_queryset

    identifiers = parse_product_ids(control.get("show_products") or control.get("product"))
    if not identifiers or len(identifiers) > MAX_CATALOG_QUOTES:
        return ()
    origin = _configured_origin()
    if origin is None:
        return ()
    base = str(getattr(settings, "SITE_BASE_URL", "") or "").rstrip("/")
    return tuple(
        f"{base}/product/{quote(slug, safe='-_.~')}/"
        for slug in public_products_queryset().filter(pk__in=identifiers).values_list("slug", flat=True)
    )


def _server_urls(
    values: Iterable[object],
    *,
    current_proposal=None,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Keep exact own-origin URLs and bind bearer checkout links to this offer."""
    origin = _configured_origin()
    if origin is None:
        return (), ("site_origin_unconfigured",), ()

    accepted: list[str] = []
    gaps: list[str] = []
    evidence: list[str] = []
    checkout_tokens: dict[str, str] = {}
    candidates: list[tuple[str, object]] = []
    for raw in islice(values or (), MAX_SERVER_URLS):
        value = str(raw or "").strip()
        if not value:
            continue
        try:
            parsed = urlsplit(value)
            candidate_origin = (
                parsed.scheme.casefold(),
                (parsed.hostname or "").casefold(),
                parsed.port,
            )
        except (TypeError, ValueError):
            _append_unique(gaps, "server_url_invalid")
            continue
        if (
            candidate_origin != origin
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            _append_unique(gaps, "server_url_not_owned")
            continue
        candidates.append((value, parsed))
        path_parts = [part for part in parsed.path.split("/") if part]
        if len(path_parts) == 3 and path_parts[:2] == ["offer", "a"]:
            checkout_tokens[value] = path_parts[2]

    valid_checkout_urls: set[str] = set()
    if checkout_tokens:
        from management.models import IgCheckoutAccessToken

        digests = {
            value: IgCheckoutAccessToken.digest(token)
            for value, token in checkout_tokens.items()
        }
        now = timezone.now()
        proposal_id = getattr(current_proposal, "pk", None)
        rows = IgCheckoutAccessToken.objects.filter(
            token_digest__in=digests.values(),
            proposal_id=proposal_id,
            revoked_at__isnull=True,
            expires_at__gt=now,
        ).values_list("token_digest", flat=True)
        valid_digests = set(rows)
        valid_checkout_urls = {
            value for value, digest in digests.items() if digest in valid_digests
        }

    for value, _parsed in candidates:
        if value in checkout_tokens and value not in valid_checkout_urls:
            _append_unique(gaps, "checkout_url_not_current")
            continue
        _append_unique(accepted, value)
        _append_unique(evidence, "configured_server_url")
    return tuple(accepted), tuple(gaps), tuple(evidence)


def _current_episode(client, *, include_completed=False):
    episode_id = getattr(client, "current_commercial_episode_id", None)
    if not getattr(client, "pk", None) or not episode_id:
        return None
    from management.models import IgCommercialEpisode

    query = (
        IgCommercialEpisode.objects.select_related(
            "deal__active_checkout_proposal",
            "deal__order",
            "intended_order",
            "primary_payment_review",
            "order_attribution__order",
        )
        .filter(pk=episode_id, client_id=client.pk)
    )
    if not include_completed:
        return query.filter(open_slot=1).first()
    episode = query.first()
    if episode is None or episode.open_slot == 1:
        return episode
    if not episode.intended_order_id or IgCommercialEpisode.objects.filter(client_id=client.pk, open_slot=1).exclude(pk=episode.pk).exists():
        return None
    return episode


def _shipping_authority(snapshot, *, source_ref):
    if not isinstance(snapshot, dict) or snapshot.get("valid") is not True:
        return ()
    mode, payer = snapshot.get("mode"), snapshot.get("payer_type")
    if mode not in {"carrier_recipient", "merchant_free", "customer_prepaid", "customer_paid_included"} or payer not in {"Sender", "Recipient"}:
        return ()
    policy = snapshot.get("shipping") or {}
    automatic = snapshot.get("authority") == "automatic_threshold"
    return (ShippingAuthority(source_ref, mode, payer, str(snapshot.get("authority") or ""),
        customer_maximum=_money(snapshot.get("customer_maximum")),
        customer_charge=_money(snapshot.get("customer_charge_amount")) if mode != "carrier_recipient" else None,
        delivery_prepaid=snapshot.get("delivery_prepaid") is True,
        policy_version=str(policy.get("policy_version") or "") if automatic else "",
        threshold=_money(policy.get("threshold")) if automatic else None,
        basis_total=_money(policy.get("basis_total")) if automatic else None,
        eligible=policy.get("eligible") if automatic and type(policy.get("eligible")) is bool else None),)


def _checkout_changes(proposal, *, now, order=None):
    from management.models import IgCheckoutRevision, IgCheckoutCartAmendment, IgCheckoutCartAmendmentResolution
    from management.services.ig_checkout_cart_amendments import verify_native_transition
    revision = IgCheckoutRevision.objects.filter(proposal=proposal, revision=proposal.revision).order_by("-pk").first()
    capture = (revision.snapshot or {}).get("source_cart_capture") or {} if revision else {}
    leaf_id = (capture.get("fence") or {}).get("native_amendment_id")
    leaf = IgCheckoutCartAmendment.objects.filter(pk=leaf_id, client_id=proposal.client_id).first() if leaf_id else None
    resolution = IgCheckoutCartAmendmentResolution.objects.filter(successor_proposal_id=proposal.pk,
        client_id=proposal.client_id).select_related("request", "decision", "execution", "successor_proposal",
            "amendment__selection_transition__session").first()
    if resolution:
        from management.services.ig_checkout_amendment_successors import _verified_resolution
        _verified_resolution(resolution, now=now)
        from management.services.ig_assisted_checkout import validate_assisted_order
        if (order is None or resolution.execution.order_id != order.pk
            or not validate_assisted_order(order, now=now).get("ready")):
            raise ValueError("checkout_amendment_paid_order_changed")
        leaf = resolution.amendment
    if leaf is None:
        if leaf_id:
            raise ValueError("checkout_amendment_leaf_missing")
        return []
    rows = list(IgCheckoutCartAmendment.objects.filter(client_id=proposal.client_id, session_id=leaf.session_id,
        created_at__lte=leaf.created_at).select_related("selection_transition__session").order_by("created_at", "pk")[:41])
    if len(rows) > 40:
        raise ValueError("checkout_amendment_history_bound")
    changes = []
    for row in rows:
        body = verify_native_transition(row.selection_transition, historical=True, now=now)
        if body.get("episode_id") != proposal.commercial_episode_id:
            raise ValueError("checkout_amendment_episode_changed")
        def items(lines):
            return [{key: line.get(key) for key in ("line_id", "title", "quantity", "product_id", "size", "color", "fit_option_code")}
                for line in lines if line.get("line_id") == body["line_id"]]
        # The signed hosted quote owns this checkout line. A selection session
        # may begin empty or use a different current line identity; it cannot
        # substitute for the basket's actual quantity before the amendment.
        before = body["previous_quote_lines"] if "previous_quote_lines" in body else (body["previous_snapshot"] or {}).get("lines", [])
        after = body["requested_quote_lines"] if "requested_quote_lines" in body else (body["next_snapshot"] or {}).get("lines", [])
        changes.append({"amendment_id": str(row.pk), "source_hmac": row.source_hmac,
            "changed_at": row.created_at.isoformat(), "operation": body["operation"], "line_id": body["line_id"],
            "quantity": body.get("quantity"), "before": items(before), "after": items(after)})
    return changes


def _current_order_ledger(client, episode, order, *, now, review=None):
    """Use the existing reconciled ledger, fenced to this exact episode/order."""
    from management.models import IgPaymentProjection
    from management.services.ig_commercial_episodes import payment_truth_snapshot
    deal = episode.deal
    review = review or episode.primary_payment_review
    if (deal is None or deal.client_id != client.pk or deal.order_id != order.pk
        or review is not None and (review.pk != episode.primary_payment_review_id
            or review.client_id != client.pk or review.deal_id != deal.pk or review.order_id not in (None, order.pk))):
        return None, True
    projection = IgPaymentProjection.objects.filter(deal_id=deal.pk).first()
    decision = review.decisions.order_by("-id").first() if review is not None else None
    if (projection is not None and projection.client_id != client.pk
        or decision is not None and (decision.client_id != client.pk or decision.review_id != review.pk)):
        return None, True
    clocks = [getattr(projection, key, None) for key in ("paid_at", "provider_modified_at", "updated_at")]
    clocks.extend((getattr(decision, "created_at", None), getattr(review, "confirmed_at", None)))
    if any(stamp is not None and stamp > now for stamp in clocks):
        return None, True
    # An empty unsaved projection records that this lookup found no provider
    # ledger, preventing the snapshot helper from doing the lookup twice.
    snapshot = payment_truth_snapshot(episode=episode, deal=deal, review=review, order=order,
        projection=projection or IgPaymentProjection(deal_id=deal.pk, client_id=client.pk),
        decision=decision, allow_deal_review_fallback=False)
    if (snapshot["episode_id"] != episode.pk or snapshot["order_id"] != order.pk
        or snapshot["deal_id"] != deal.pk or snapshot["currency"] != "UAH"):
        return None, True
    blocked = bool(snapshot["needs_reconciliation"] or snapshot["provider_truth"] in {
        "refunded", "partially_refunded", "reversed", "failed", "cancelled"})
    if (not blocked and snapshot["authoritative_for_fulfillment"]
        and snapshot["reconciliation_state"] == "manager_verified_provider_unverified"):
        confirmed_at = getattr(review, "confirmed_at", None) or getattr(decision, "created_at", None)
        snapshot["manager_confirmed_at"] = confirmed_at.isoformat() if confirmed_at is not None else None
    return snapshot, blocked


def capture_checkout_order_authority(client, *, episode=None, order=None, proposal=None, review=None, now=None):
    """Read only the exact native winning graph and actual materialized rows.

    Already resolved episode/order/proposal avoid duplicate ownership lookups.
    Every supplied object must still belong to this client's current episode.
    """
    from management.models import IgCheckoutInvoiceGeneration
    from orders.models import PaymentAttempt
    from orders.services.delivery_payment import delivery_payment_snapshot, threshold_shipping_cart_digest
    now = now or timezone.now()
    try:
        episode = episode or _current_episode(client, include_completed=True)
        if episode is None or episode.pk != client.current_commercial_episode_id or episode.client_id != client.pk:
            return None
        native_order, gap = _episode_order(episode)
        order = order or native_order
        if gap or order is None or native_order is None or native_order.pk != order.pk:
            return None
        proposal = proposal or _current_proposal(episode)
        if (proposal is None or proposal.client_id != client.pk or proposal.commercial_episode_id != episode.pk
            or proposal.deal_id != episode.deal_id or proposal.status != "paid"
            or episode.intended_order_id != order.pk or episode.deal.order_id != order.pk):
            return None
        generation = None
        if proposal.winner_invoice_generation_id:
            generation = IgCheckoutInvoiceGeneration.objects.select_related("payment_attempt").filter(
                pk=proposal.winner_invoice_generation_id, proposal_id=proposal.pk, proposal_revision=proposal.revision,
                winner_slot=1, state="paid_winner").first()
            attempt = generation.payment_attempt if generation else None
        else:
            attempt = PaymentAttempt.objects.filter(pk=proposal.payment_attempt_id, order_id=order.pk).first()
        if (attempt is None or attempt.order_id != order.pk or attempt.status not in {"paid", "prepaid", "converted"}
            or _money(attempt.gross_amount) != _money(order.total_sum)
            or _money(attempt.discount_amount) != _money(order.discount_amount)):
            return None
        rows = list(order.items.select_related("color_variant__color").order_by("pk")[:41])
        if not 1 <= len(rows) <= 40:
            return None
        digest = threshold_shipping_cart_digest(rows)
        cart = (attempt.cart_snapshot or {}).get("cart")
        if not isinstance(cart, list) or threshold_shipping_cart_digest(cart) != digest:
            return None
        # Reuse this one verified snapshot with the already read actual rows.
        # Calling order_amounts first would repeat native Attempt/P/Generation
        # verification and could consume a different item read in the same pass.
        snapshot = delivery_payment_snapshot(order, item_rows=rows)
        from management.services.ig_assisted_partial_shipping import assisted_shipping_snapshot
        snapshot = assisted_shipping_snapshot(order, snapshot)
        gross, discount = _money(order.total_sum), _money(order.discount_amount)
        merchandise = max(gross - discount, Decimal("0"))
        if snapshot.get("mode") == "customer_paid_included" and snapshot.get("valid") is True:
            merchandise = _money(snapshot["merchandise_total"])
            payable = _money(snapshot["payable_total"])
        else:
            payable = merchandise + _money(snapshot["delivery_amount"])
        amounts = {"subtotal": gross, "discount": discount, "merchandise_payable": merchandise, "payable": payable}
        if amounts["payable"] != _money(attempt.payable_amount):
            return None
        items = [{"line_id": str(row.pk), "title": row.title, "quantity": row.qty,
            "unit_price": str(row.unit_price), "line_total": str(row.line_total), "size": row.size or "",
            "fit": row.fit_option_code or "", "color": row.color_name_custom or
                (row.color_variant.color.name if row.color_variant_id and row.color_variant else "")} for row in rows]
        paid = _money(attempt.paid_amount) if attempt.paid_amount is not None else None
        paid_at = proposal.paid_at.isoformat() if proposal.paid_at and proposal.paid_at <= now else None
        ledger, blocked = _current_order_ledger(client, episode, order, now=now, review=review)
        if blocked:
            paid = None
        elif ledger is not None and ledger["authoritative_for_fulfillment"]:
            if _money(ledger["actual_order_total"]) != payable:
                paid = None
            else:
                paid = _money(ledger["confirmed_paid_amount"])
                if ledger["reconciliation_state"] == "manager_verified_provider_unverified":
                    paid_at = ledger.get("manager_confirmed_at")
        shipping = _shipping_authority(snapshot, source_ref=f"order:{order.pk}:attempt:{attempt.pk}")
        changes = _checkout_changes(proposal, now=now, order=order)
        body = {"schema": CHECKOUT_ORDER_SCHEMA, "scope": {"client_id": client.pk, "episode_id": episode.pk, "order_id": order.pk},
            "order_number": order.order_number, "winner_generation_id": generation.pk if generation else None,
            "attempt_id": attempt.pk, "observed_at": now.isoformat(), "items": items,
            "money": {"gross": str(amounts["subtotal"]), "discount": str(amounts["discount"]),
                "merchandise_payable": str(amounts["merchandise_payable"]), "payable": str(amounts["payable"]),
                "confirmed_paid": str(paid) if paid is not None else None,
                "balance": str(max(amounts["payable"] - paid, Decimal("0"))) if paid is not None else None, "currency": "UAH"},
            "checkout_changes": changes, "shipping_authorities": [_shipping_wire(fact) for fact in shipping],
            "source_ref": {"proposal_id": proposal.pk, "proposal_revision": proposal.revision,
                "generation_id": generation.pk if generation else None, "attempt_id": attempt.pk,
                "items_digest": digest, "settlement_status": attempt.status,
                "amendment_hmacs": [row["source_hmac"] for row in changes],
                "paid_at": paid_at}}
        if ledger is not None:
            body["source_ref"]["payment_ledger"] = {"review_id": ledger["review_id"],
                "decision_id": ledger["decision_id"], "projection_id": ledger["projection_id"],
                "reconciliation_state": ledger["reconciliation_state"],
                "needs_reconciliation": ledger["needs_reconciliation"],
                "digest": hashlib.sha256(json.dumps(ledger, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}
        key_id = next(iter(_snapshot_keys()))
        return checkout_order_authority({**body, "key_id": key_id, "hmac": _snapshot_mac(body, key_id)},
            client_id=client.pk, episode_id=episode.pk)
    except (ValueError, TypeError, KeyError, InvalidOperation):
        return None


def _pending_shipping_authorities(client, episode, proposal):
    """Current native pending economics, never a catalogue or provisional wish."""
    from management.models import IgCheckoutInvoiceGeneration
    from orders.models import PaymentAttempt
    from orders.services.delivery_payment import build_threshold_delivery_contract
    from orders.services.shipping_policy import build_shipping_snapshot
    if (proposal is None or proposal.client_id != client.pk or proposal.commercial_episode_id != episode.pk
        or proposal.status not in {"ready", "viewed", "details_locked", "invoice_created"}):
        return ()
    assisted = getattr(proposal, "assisted_commerce_execution", None)
    if assisted is not None and assisted.scope == "partial":
        return ()
    try:
        generation = IgCheckoutInvoiceGeneration.objects.filter(proposal_id=proposal.pk, active_slot=1,
            proposal_revision=proposal.revision).select_related("payment_attempt").first()
        attempt = generation.payment_attempt if generation else (
            PaymentAttempt.objects.filter(pk=proposal.payment_attempt_id).first() if proposal.payment_attempt_id else None)
        if attempt is not None:
            contract = build_threshold_delivery_contract(attempt, proposal=proposal, generation=generation)
            if (attempt.cart_snapshot or {}).get("threshold_delivery_contract") != contract:
                return ()
            shipping = contract["shipping"]
        else:
            shipping = build_shipping_snapshot(merchandise_total=proposal.quoted_total,
                gross_total=proposal.catalog_total, discount_amount=proposal.negotiated_discount,
                cart_digest=proposal.items_digest, owner={"client_id": client.pk, "episode_id": episode.pk,
                    "proposal_pk": proposal.pk, "proposal_id": str(proposal.public_id), "revision": proposal.revision})
        return _shipping_authority({"valid": True, "mode": shipping["mode"], "payer_type": shipping["payer_type"],
            "authority": "automatic_threshold", "shipping": shipping, "delivery_prepaid": False,
            "customer_charge_amount": "0.00" if shipping["eligible"] else None},
            source_ref=f"proposal:{proposal.pk}:revision:{proposal.revision}")
    except (ValueError, TypeError, KeyError, InvalidOperation):
        return ()


def _episode_order(episode):
    candidates = []
    intended = getattr(episode, "intended_order", None)
    if intended is not None:
        candidates.append(intended)
    attribution = getattr(episode, "order_attribution", None)
    attributed = getattr(attribution, "order", None) if attribution else None
    if attributed is not None:
        candidates.append(attributed)
    deal = getattr(episode, "deal", None)
    deal_order = getattr(deal, "order", None) if deal else None
    if deal_order is not None:
        candidates.append(deal_order)
    ids = {row.pk for row in candidates if getattr(row, "pk", None)}
    if len(ids) > 1:
        return None, "current_order_conflict"
    return (candidates[0] if candidates else None), ""


def _payment_confirmed(client, episode, order) -> tuple[bool, str]:
    from management.services.bot_payment_truth import (
        CONFIRMED_ORDER_PAYMENT_STATUSES,
        current_manager_confirmation_review_q,
        verified_payment_deals,
    )

    deal_id = getattr(episode, "deal_id", None)
    if deal_id and verified_payment_deals(
        client.deals.filter(pk=deal_id)
    ).exists():
        return True, "current_episode_provider_payment"
    review_id = getattr(episode, "primary_payment_review_id", None)
    if review_id and client.payment_confirmation_reviews.filter(
        current_manager_confirmation_review_q(), pk=review_id
    ).exists():
        return True, "current_episode_manager_payment"
    if order is not None and str(order.payment_status or "") in (
        CONFIRMED_ORDER_PAYMENT_STATUSES
    ):
        return True, "current_episode_paid_order"
    return False, ""


def _current_proposal(episode):
    deal = getattr(episode, "deal", None)
    proposal = getattr(deal, "active_checkout_proposal", None) if deal else None
    if proposal is None or proposal.commercial_episode_id != episode.pk:
        return None
    from management.models import IgCheckoutProposal

    active = {
        IgCheckoutProposal.Status.READY,
        IgCheckoutProposal.Status.VIEWED,
        IgCheckoutProposal.Status.DETAILS_LOCKED,
        IgCheckoutProposal.Status.INVOICE_CREATED,
        IgCheckoutProposal.Status.MANAGER_REVIEW,
        IgCheckoutProposal.Status.PAID,
    }
    if proposal.status not in active:
        return None
    if proposal.status != IgCheckoutProposal.Status.PAID and proposal.expires_at <= timezone.now():
        return None
    return proposal


def _resolved_catalog_quote(client, candidate: Mapping[str, object] | None):
    if not isinstance(candidate, Mapping) or "price_quoted" not in candidate:
        return None
    from management.services.instagram_bot import _validated_price_quote

    catalog_candidate = dict(candidate)
    # ``price`` is the legacy negotiated-price alias.  Passing it through
    # would let `_validated_price_quote` consult conversational evidence before
    # catalog pricing, which is outside this factory's authority contract.
    catalog_candidate.pop("price", None)
    resolved = _validated_price_quote(client, catalog_candidate)
    if not isinstance(resolved, Mapping) or resolved.get("price_source") != "catalog":
        return None
    return resolved


def _current_configuration_pricing(client, control: Mapping[str, object] | None):
    """Resolve at most one published product/configuration without model prices."""
    if not getattr(client, "pk", None):
        return None, False
    candidate = dict(control) if isinstance(control, Mapping) else {}
    if candidate.get("_invalid"):
        return None, True
    from management.services.instagram_bot import (
        _checkout_selection_state,
        _control_option_values,
        _control_product_id,
    )
    from storefront.models import Product, ProductStatus

    supplied_product_values = [
        candidate.get(key)
        for key in ("product", "product_id")
        if key in candidate
    ]
    explicit_product = bool(supplied_product_values)
    if len(supplied_product_values) > 1 and len({
        str(value).strip() for value in supplied_product_values
    }) > 1:
        return None, True
    product_candidate = (
        {"product": supplied_product_values[0]} if explicit_product else {}
    )
    product_id = _control_product_id(product_candidate) if explicit_product else None
    if not product_id:
        if explicit_product:
            return None, True
        product_id = getattr(client, "current_product_id", None)
    try:
        product_id = int(product_id or 0)
    except (TypeError, ValueError):
        return None, bool(explicit_product)
    if not product_id:
        return None, False
    product = Product.objects.filter(
        pk=product_id,
        status=ProductStatus.PUBLISHED,
    ).first()
    if product is None:
        return None, True

    selection = _checkout_selection_state(client, product_id)
    explicit_variant = None
    variant_supplied = False
    for key in ("color_variant_id", "variant"):
        if key in candidate:
            variant_supplied = True
            explicit_variant = candidate.get(key)
            break
    if variant_supplied:
        if isinstance(explicit_variant, bool):
            return None, True
        try:
            variant_id = int(explicit_variant or 0)
        except (TypeError, ValueError):
            return None, True
        if variant_id <= 0:
            return None, True
    else:
        try:
            variant_id = int(selection.get("color_variant_id") or 0)
        except (TypeError, ValueError):
            return None, True

    proposed_options = _control_option_values(candidate)
    if proposed_options is None:
        return None, True
    persisted_options = selection.get("option_values")
    options = (
        dict(persisted_options)
        if isinstance(persisted_options, Mapping)
        else {}
    )
    options.update(proposed_options)
    fit = str(candidate.get("fit") or "").strip().casefold()
    if fit:
        if "fit" in proposed_options and proposed_options["fit"] != fit:
            return None, True
        options["fit"] = fit
    elif selection.get("fit_option_code") and "fit" not in options:
        options["fit"] = str(selection["fit_option_code"]).strip().casefold()

    from management.services.ig_catalog_pricing import resolve_product_pricing

    variants = list(
        product.color_variants.select_related("color").order_by("order", "id")
    )
    if variant_id and not any(variant.pk == variant_id for variant in variants):
        return None, True
    pricing = resolve_product_pricing(
        product,
        variants=variants,
        selected_variant_id=variant_id or None,
        option_values=options or None,
    )
    if pricing.get("minimum") is None or pricing.get("maximum") is None:
        return None, True
    return pricing, False


def _shipment_truth(order) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    if order is None:
        return "unknown", (), ()
    from management.models import IgOrderShipment
    from orders.fulfillment_truth import nova_poshta_delivery_confirmed_at

    tracking: list[str] = []
    if str(order.tracking_number or "").strip():
        tracking.append(str(order.tracking_number).strip())
    for value in IgOrderShipment.objects.filter(
        order_id=order.pk,
        source__in=(
            IgOrderShipment.Source.ORDER_FIELD,
            IgOrderShipment.Source.MANAGER_MANUAL,
        ),
    ).order_by("created_at", "id").values_list("tracking_number", flat=True)[:16]:
        value = str(value or "").strip()
        if value:
            _append_unique(tracking, value)
    if nova_poshta_delivery_confirmed_at(order) is not None:
        return "delivered", tuple(tracking), ("current_order_carrier_delivery",)
    if order.status == "ship" and tracking:
        return "shipped", tuple(tracking), ("current_order_shipped",)
    if order.status == "prep":
        # The Order label is "preparing for shipment".  It does not prove the
        # positive customer-facing claim "ready to ship".
        return "preparing", tuple(tracking), ("current_order_preparing",)
    return "unknown", tuple(tracking), ()


def _conversation_facts(client):
    context = client.sales_context if isinstance(client.sales_context, dict) else {}
    stored = context.get("conversation_agreement")
    if not isinstance(stored, dict):
        return (), ()
    from management.models import InstagramBotSettings, InstagramBotMessage
    from management.services.instagram_bot import ingress_provider_namespace
    from management.services.ig_conversation_routes import conversation_route_reset_floor
    from management.services.ig_conversation_agreement import MAX_MESSAGES, _row, extract_conversation_agreement, read_conversation_agreement
    from management.services.ig_memory_producer import _namespaces, _source_allowed
    settings_row = InstagramBotSettings.objects.filter(pk=1).first()
    if settings_row is None:
        return (), ()
    captured = read_conversation_agreement(client,
        episode_id=client.current_commercial_episode_id,
        source_namespace=ingress_provider_namespace(settings_row),
        reset_floor=conversation_route_reset_floor(client.pk))
    agreement = captured.get("agreement") or {}
    if not agreement:
        return (), ()
    new_sources = list(InstagramBotMessage.objects.filter(client_id=client.pk,
        pk__gt=agreement["watermark_message_id"], status="done").order_by("pk")[:MAX_MESSAGES + 1])
    if new_sources:
        retained = captured.get("source_rows") or []
        if len(retained) + len(new_sources) > MAX_MESSAGES:
            return (), ()
        namespace = ingress_provider_namespace(settings_row)
        namespaces = _namespaces(new_sources)
        if any(source.sender_id != client.igsid or namespaces.get(source.pk) != namespace
                or not _source_allowed(source) or (source.provider_created_at or source.created_at) > timezone.now()
                for source in new_sources):
            return (), ()
        reproduced = extract_conversation_agreement([*retained,
            *[{**_row(source), "provider_namespace": namespaces[source.pk]} for source in new_sources]])
        if any(reproduced.get(key) != agreement.get(key) for key in ("items", "amounts", "shipping_payment")):
            return (), ()
    amounts = agreement.get("amounts") or {}
    if amounts.get("authority") not in {"conversation_agreement", "seller_instruction"}:
        amounts = {}
    if any(reason in (agreement.get("uncertainty_reasons") or []) for reason in (
        "amount_arithmetic_mismatch", "multi_item_price_allocation_required", "conversation_price_allocation_required")):
        amounts = {}
    ids = agreement.get("source_message_ids") or []
    result = []
    currency = amounts.get("currency")
    if currency not in {"UAH", "USD", "EUR"}:
        amounts = {}
    for kind, key in (("merchandise", "merchandise_total"), ("delivery", "delivery_amount"), ("payable", "payable_total")):
        value = amounts.get(key)
        source_id = amounts.get("delivery_source_message_id") if kind == "delivery" else amounts.get("source_message_id")
        source_id = source_id or amounts.get("source_message_id")
        amount = _money(value) if value not in (None, "") else None
        if amount is not None and amount.is_finite() and type(source_id) is int and source_id in ids:
            result.append((kind, f"{amount:.2f}", currency, source_id, amounts["authority"]))
    choices = []
    items = agreement.get("items") or []
    if len(items) == 1:
        item = items[0]
        acceptance = item.get("acceptance_message_id")
        if (item.get("configuration_authority") == "customer_confirmed_seller_offer"
                and type(acceptance) is int and acceptance in ids):
            from management.services.ig_response_plan import CHOICE_ALIASES
            for axis, key in (("size", "size"), ("fit", "fit"), ("color", "color")):
                value = item.get(key)
                if isinstance(value, str) and value:
                    choices.extend((axis, alias, acceptance) for alias in CHOICE_ALIASES.get(value, (value,)))
    return tuple(result), tuple(choices)


def fulfillment_timing_authorities(projection: Mapping[str, object]) -> tuple[str, tuple[TimingAuthority, ...]]:
    """Adapt a verified current projection without granting carrier promises.

    Callers must supply the verified projection from the frozen client state;
    model controls, raw card fields and a stored snapshot are not authority.
    """
    if not isinstance(projection, Mapping) or type(projection.get("version")) is not int or projection.get("version") != 1:
        return "", ()
    request = projection.get("current_request", projection)
    if not isinstance(request, Mapping) or request.get("scope") != "cart":
        return "", ()
    status = projection.get("status")
    if not isinstance(status, str) or status not in {"pending", "reviewed", "stale"}:
        return "", ()
    if status != "reviewed":
        return "pending", ()
    decision = projection.get("decision")
    lines = request.get("lines")
    digest = projection.get("request_digest", projection.get("digest"))
    if (
        not isinstance(decision, Mapping) or not isinstance(lines, list)
        or not isinstance(digest, str) or not digest
        or decision.get("request_digest") != digest
        or not isinstance(decision.get("lines"), list)
    ):
        return "pending", ()
    requested = {
        str(line.get("line_id")): line
        for line in lines[:40] if isinstance(line, Mapping) and line.get("line_id")
    }
    reviewed = {
        str(line.get("line_id")): line
        for line in decision["lines"][:40] if isinstance(line, Mapping) and line.get("line_id")
    }
    if not requested or set(requested) != set(reviewed) or len(requested) != len(lines) or len(reviewed) != len(decision["lines"]):
        return "pending", ()
    line_labels = tuple((line_id, str(row.get("label") or "").strip()[:240])
        for line_id, row in requested.items())
    result: list[TimingAuthority] = []
    for domain in ("preparation", "dispatch", "base_arrival"):
        domain_facts: list[TimingAuthority] = []
        for line_id, row in reviewed.items():
            timing = row.get(domain)
            if row.get("availability") not in ("available", "wait") or not isinstance(timing, Mapping):
                continue
            if domain == "base_arrival" and (
                row.get("availability") != "wait" or row.get("supply_basis") != "incoming_confirmed"
            ):
                continue
            low, high = timing.get("min_days"), timing.get("max_days")
            raw_unit = timing.get("unit")
            unit = {"calendar": "calendar_days", "business": "business_days"}.get(raw_unit) if isinstance(raw_unit, str) else None
            anchor_at = timing.get("anchor_at")
            anchor = timing.get("anchor", "")
            payment_anchor = anchor == "after_payment_confirmation" and anchor_at in (None, "")
            if (type(low) is not int or type(high) is not int or not 0 <= low <= high <= 365
                    or not unit or anchor not in ("", "after_payment_confirmation")
                    or not payment_anchor and (anchor or not isinstance(anchor_at, str) or not anchor_at)):
                continue
            if domain == "base_arrival":
                if high > 90 or anchor or not isinstance(anchor_at, str):
                    continue
                try:
                    clock = datetime.fromisoformat(anchor_at.replace("Z", "+00:00"))
                    if timezone.is_naive(clock):
                        continue
                except (ValueError, TypeError, OverflowError):
                    continue
            domain_facts.append(TimingAuthority(
                domain=domain, min_days=low, max_days=high, unit=unit,
                anchor=anchor, anchor_at=anchor_at or "", line_id=line_id,
                line_label=str(requested[line_id].get("label") or "").strip()[:240],
                source_ref=digest, review_line_labels=line_labels,
            ))
        result.extend(domain_facts)
        # A fact about one item does not authorize a whole-order promise. Only
        # a uniform full set can be spoken without an exact item label.
        windows = {(fact.min_days, fact.max_days, fact.unit, fact.anchor, fact.anchor_at) for fact in domain_facts}
        if domain != "base_arrival" and len(domain_facts) == len(requested) and len(windows) == 1:
            fact = domain_facts[0]
            result.append(TimingAuthority(
                domain=domain, min_days=fact.min_days, max_days=fact.max_days,
                unit=fact.unit, anchor=fact.anchor, anchor_at=fact.anchor_at, source_ref=digest,
                review_line_labels=line_labels,
            ))
    return "reviewed", tuple(result)


def fulfillment_deadline_authorities(projection) -> tuple[FulfillmentDeadlineAuthority, ...]:
    """Read only the frozen, verified review and its exact customer requirement.

    The adapter does not query live state or bless notes/model controls. The
    capture producer owns signature, epoch, cart and source revalidation.
    """
    status, _ = fulfillment_timing_authorities(projection)
    if status != "reviewed":
        return ()
    from management.services.ig_fulfillment_requirements import active_hard_requirements, requirement_digest
    request = projection.get("current_request", projection)
    decision = projection["decision"]
    review = decision.get("deadline_review")
    if (not isinstance(review, Mapping)
            or set(review) != {"requirement_digest", "outcome", "line_ids", "options"}
            or review.get("outcome") not in {"impossible", "review_pending"}):
        return ()
    line_ids, options = review.get("line_ids"), review.get("options")
    requested = {row["line_id"] for row in request["lines"]}
    if (not isinstance(line_ids, list) or not line_ids
            or any(not isinstance(value, str) for value in line_ids)
            or len(set(line_ids)) != len(line_ids) or not set(line_ids).issubset(requested)
            or not isinstance(options, list) or any(not isinstance(value, str) or value not in {
                "later_domestic_contact", "later_domestic_destination", "international_review_required",
            } for value in options) or len(set(options)) != len(options)):
        return ()
    requirements = request.get("customer_requirements")
    if not isinstance(requirements, list):
        return ()
    matches = [row for row in active_hard_requirements(requirements)
        if row.get("requirement_digest") == review.get("requirement_digest")
        and requirement_digest(row) == review.get("requirement_digest")]
    if len(matches) != 1:
        return ()
    requirement = matches[0]
    source_id, kind, deadline = requirement.get("source_id"), requirement.get("deadline_kind"), requirement.get("deadline_at")
    if (type(source_id) is not int or source_id < 1 or kind not in {"departure", "arrival", "dispatch"}
            or not isinstance(deadline, str)):
        return ()
    try:
        clock = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
        if timezone.is_naive(clock):
            return ()
    except (ValueError, TypeError, OverflowError):
        return ()
    return (FulfillmentDeadlineAuthority(
        requirement_digest=review["requirement_digest"], deadline_at=deadline, deadline_kind=kind,
        outcome=review["outcome"], line_ids=tuple(line_ids), source_message_id=source_id,
        source_ref=projection.get("request_digest", projection.get("digest")), options=tuple(options),
    ),)


def fulfillment_availability_authorities(projection) -> tuple[AvailabilityAuthority, ...]:
    """Exact dialogue facts from a verified manager decision, never DB stock."""
    status, _ = fulfillment_timing_authorities(projection)
    if status != "reviewed":
        return ()
    request = projection.get("current_request", projection)
    decision = projection["decision"]
    digest = projection.get("request_digest", projection.get("digest"))
    requested = {str(row["line_id"]): row for row in request["lines"]}
    facts = []
    for row in decision["lines"]:
        source = requested[str(row["line_id"])]
        target = source.get("target")
        if not isinstance(target, Mapping) or set(target) != {
            "product_id", "color_variant_id", "size", "fit_option_code", "option_values", "quantity",
        }:
            return ()
        if any(type(target.get(key)) is not int or target[key] <= 0 for key in ("product_id", "color_variant_id")):
            return ()
        quantity, options = target.get("quantity"), target.get("option_values")
        if (type(quantity) is not int or not 1 <= quantity <= 99
                or not isinstance(target.get("size"), str) or not target["size"]
                or not isinstance(target.get("fit_option_code"), str)
                or not isinstance(options, Mapping)
                or any(not isinstance(key, str) or not isinstance(value, str) for key, value in options.items())):
            return ()
        observation = source.get("observation")
        labels = observation.get("labels") if isinstance(observation, Mapping) else {}
        labels = labels if isinstance(labels, Mapping) else {}
        checked = row.get("checked_quantity")
        facts.append(AvailabilityAuthority(
            line_id=str(row["line_id"]), line_label=str(source.get("label") or "")[:240],
            product_id=target["product_id"], color_variant_id=target["color_variant_id"],
            size=target["size"], fit_code=target["fit_option_code"], quantity=quantity,
            option_values=tuple(sorted(options.items())), availability=str(row.get("availability") or ""),
            checked_quantity=checked if type(checked) is int else None,
            supply_basis=str(row.get("supply_basis") or ""), source_ref=digest,
            product_label=str(labels.get("product") or "")[:160],
            color_label=str(labels.get("color") or "")[:80],
            fit_label=str(labels.get("fit") or "")[:80],
        ))
    return tuple(facts)


def _captured_fulfillment_projection(captured_client_state):
    payload = captured_client_state.as_dict() if hasattr(captured_client_state, "as_dict") else captured_client_state
    if not isinstance(payload, Mapping) or payload.get("status") != "captured":
        return None
    diagnostics = payload.get("diagnostics")
    if isinstance(diagnostics, Mapping) and diagnostics.get("fulfillment_timing_hold") is True:
        # Capture proved an active exact purchase review but could not admit
        # its timing facts. Unknown authority must not resurrect 1–3 dispatch.
        return {"version": 1, "scope": "cart", "status": "pending"}
    slots = payload.get("slots")
    slot = slots.get("fulfillment.review") if isinstance(slots, Mapping) else None
    if isinstance(slot, Mapping):
        if slot.get("status") != "confirmed" or slot.get("omission_reason"):
            return None
        return slot.get("value")
    return payload.get("fulfillment_review")


def _fulfillment_timing_truth(captured_client_state) -> tuple[str, tuple[TimingAuthority, ...]]:
    return fulfillment_timing_authorities(_captured_fulfillment_projection(captured_client_state))


def apply_fulfillment_timing_context(context: ReplyTruthContext, captured_client_state) -> ReplyTruthContext:
    """Overlay the same frozen review used by the prompt and response plan."""
    status, facts = _fulfillment_timing_truth(captured_client_state)
    if not status:
        return context
    independent = tuple(fact for fact in context.timing_authorities
        if isinstance(fact, TimingAuthority) and fact.domain in {"carrier_estimate", "arrival_feasibility"})
    return replace(context, timing_authorities=(*independent, *facts),
        fulfillment_deadline_authorities=fulfillment_deadline_authorities(_captured_fulfillment_projection(captured_client_state)),
        availability_authorities=fulfillment_availability_authorities(_captured_fulfillment_projection(captured_client_state)),
        fulfillment_review_status=status, explicitly_qualified_standard_dispatch_days=None)


def build_reply_truth_context(
    client,
    *,
    control: Mapping[str, object] | None = None,
    server_urls: Iterable[object] = (),
    catalog_quotes: Iterable[Mapping[str, object]] = (),
    server_timing_claims: Iterable[object] = (),
    captured_client_state=None,
    checkout_authority=None,
) -> ReplyTruthContext:
    """Return current-episode facts suitable for ``validate_reply_truth``.

    ``server_urls``, ``catalog_quotes`` and ``server_timing_claims`` must be
    application-produced inputs.  The factory never extracts them from model
    prose.  Actions remain owned by the existing business gates.
    """
    gaps: list[str] = []
    evidence: list[str] = []
    prices: list[Decimal] = []
    ranges: list[tuple[Decimal, Decimal]] = []
    discount_amounts: list[Decimal] = []
    sizes: list[str] = []
    fits: list[str] = []
    colors: list[str] = []
    currencies: list[str] = ["UAH"]

    episode = _current_episode(client, include_completed=True)
    order = None
    proposal = None
    payment_confirmed = False
    order_created = False
    shipment_state = "unknown"
    tracking_refs: tuple[str, ...] = ()
    actual_checkout = None
    legacy_full_payment = False
    shipping_authorities = ()
    if episode is None:
        _append_unique(gaps, "current_episode_unavailable")
    else:
        _append_unique(evidence, "current_episode")
        order, order_gap = _episode_order(episode)
        if order_gap:
            _append_unique(gaps, order_gap)
        order_created = order is not None
        if order_created:
            _append_unique(evidence, "current_episode_order")
        payment_confirmed, payment_evidence = _payment_confirmed(
            client, episode, order
        )
        if payment_evidence:
            _append_unique(evidence, payment_evidence)
        shipment_state, tracking_refs, shipment_evidence = _shipment_truth(order)
        for code in shipment_evidence:
            _append_unique(evidence, code)

        proposal = _current_proposal(episode)
        if order is not None:
            if isinstance(checkout_authority, CheckoutOrderAuthority):
                actual_checkout = checkout_order_authority(checkout_authority.as_dict(), client_id=client.pk, episode_id=episode.pk)
                if actual_checkout is not None and actual_checkout.order_id != order.pk:
                    actual_checkout = None
            else:
                actual_checkout = capture_checkout_order_authority(client, episode=episode, order=order, proposal=proposal)
            if actual_checkout is None:
                _append_unique(gaps, "current_checkout_order_unverified")
                if proposal is None or not proposal.winner_invoice_generation_id and not proposal.payment_attempt_id:
                    # Manager-created Orders have no bank-winner receipt. Their
                    # exact current materialized economics remain actual facts.
                    from management.services.ig_order_amounts import order_amounts
                    amounts = order_amounts(order)
                    rows = list(order.items.select_related("color_variant__color").order_by("pk")[:41])
                    if 1 <= len(rows) <= 40 and sum((row.line_total for row in rows), Decimal("0")) == amounts["subtotal"]:
                        for value in (amounts["merchandise_payable"], amounts["payable"]):
                            if value > 0:
                                _append_unique(prices, value)
                        if amounts["discount"] > 0:
                            _append_unique(discount_amounts, amounts["discount"])
                        for item in rows:
                            for value in (item.unit_price, item.line_total):
                                if value > 0:
                                    _append_unique(prices, value)
                            color = item.color_name_custom or (item.color_variant.color.name
                                if item.color_variant_id and item.color_variant else "")
                            for target, value in ((sizes, item.size), (fits, item.fit_option_code), (colors, color)):
                                if value:
                                    _append_unique(target, value)
                    ledger, blocked = _current_order_ledger(client, episode, order, now=timezone.now())
                    payment_confirmed = bool(not blocked and ledger and ledger["authoritative_for_fulfillment"])
                    legacy_full_payment = bool(payment_confirmed and _money(ledger["confirmed_paid_amount"]) is not None
                        and _money(ledger["confirmed_paid_amount"]) >= amounts["payable"] > 0)
            else:
                _append_unique(evidence, "current_native_checkout_order")
                shipping_authorities = actual_checkout.shipping_authorities
                payment_confirmed = actual_checkout.confirmed_paid is not None and actual_checkout.confirmed_paid > 0
                for value in (actual_checkout.merchandise_payable, actual_checkout.payable):
                    if value > 0:
                        _append_unique(prices, value)
                if actual_checkout.discount > 0:
                    _append_unique(discount_amounts, actual_checkout.discount)
                for item in actual_checkout.items:
                    for value in (item.unit_price, item.line_total):
                        if value > 0:
                            _append_unique(prices, value)
                    for target, value in ((sizes, item.size), (fits, item.fit), (colors, item.color)):
                        if value:
                            _append_unique(target, value)
        elif proposal is not None:
            _append_unique(evidence, "current_episode_proposal")
            shipping_authorities = _pending_shipping_authorities(client, episode, proposal)
            currency = str(proposal.currency or "").strip().upper()
            if currency:
                currencies = [currency]
            for value in (
                proposal.catalog_total,
                proposal.quoted_total,
            ):
                amount = _money(value)
                if amount is not None and amount > 0:
                    _append_unique(prices, amount)
            discount = _money(proposal.negotiated_discount)
            if discount is not None and discount > 0:
                _append_unique(discount_amounts, discount)
            for item in proposal.items.all().order_by("position", "id")[:40]:
                for value in (
                    item.catalog_unit_price,
                    item.catalog_line_total,
                    item.quoted_unit_price,
                    item.quoted_line_total,
                ):
                    amount = _money(value)
                    if amount is not None and amount > 0:
                        _append_unique(prices, amount)
                for target, value in (
                    (sizes, item.size),
                    (fits, item.fit_code),
                    (colors, item.color_code),
                    (colors, item.color_label),
                ):
                    normalized = str(value or "").strip()
                    if normalized:
                        _append_unique(target, normalized)

    configuration_pricing, configuration_invalid = (_current_configuration_pricing(client, control)
        if order is None else (None, False))
    if configuration_invalid:
        _append_unique(gaps, "current_configuration_unverified")
    elif configuration_pricing is not None:
        minimum = _money(configuration_pricing.get("minimum"))
        maximum = _money(configuration_pricing.get("maximum"))
        if minimum is not None and maximum is not None:
            if configuration_pricing.get("exact") and minimum > 0:
                _append_unique(prices, minimum)
            elif 0 < minimum < maximum:
                _append_unique(ranges, (minimum, maximum))
            _append_unique(evidence, "current_configuration_price")

    # A current published product/variant price is independent of whether the
    # customer has opened a commercial episode.  The candidate still has no
    # authority by itself: the existing exact resolver must reproduce it.
    quote_candidates: list[Mapping[str, object]] = []
    if isinstance(control, Mapping) and "price_quoted" in control:
        quote_candidates.append(control)
    quote_candidates.extend(islice(catalog_quotes or (), MAX_CATALOG_QUOTES))
    resolved_quote_amounts: list[Decimal] = []
    for candidate in (quote_candidates[:MAX_CATALOG_QUOTES] if order is None else ()):
        resolved = _resolved_catalog_quote(client, candidate)
        amount = _money(resolved.get("amount")) if resolved else None
        if amount is None or amount <= 0:
            _append_unique(gaps, "catalog_quote_unverified")
            continue
        _append_unique(prices, amount)
        _append_unique(resolved_quote_amounts, amount)
        _append_unique(evidence, "exact_catalog_quote")
        fit = str(resolved.get("fit_option_code") or "").strip()
        if fit:
            _append_unique(fits, fit)
    if len(resolved_quote_amounts) > 1:
        low, high = min(resolved_quote_amounts), max(resolved_quote_amounts)
        if low != high:
            ranges.append((low, high))

    authorized_urls, url_gaps, url_evidence = _server_urls(
        (*_catalog_control_urls(control), *islice(server_urls or (), MAX_SERVER_URLS)),
        current_proposal=proposal,
    )
    for code in url_gaps:
        _append_unique(gaps, code)
    for code in url_evidence:
        _append_unique(evidence, code)

    timing: list[str] = []
    for value in islice(server_timing_claims or (), MAX_TIMING_CLAIMS):
        normalized = " ".join(str(value or "").split())[:160]
        if normalized:
            _append_unique(timing, normalized)
    if timing:
        _append_unique(evidence, "explicit_server_timing")
    conversation_amounts, agreement_choices = _conversation_facts(client)
    if order is not None:
        conversation_amounts, agreement_choices = (), ()
    if conversation_amounts:
        _append_unique(evidence, "source_proved_conversation_amounts")
    fulfillment_status, timing_authorities = _fulfillment_timing_truth(captured_client_state)
    if fulfillment_status == "pending":
        _append_unique(evidence, "source_bound_fulfillment_pending")
    elif fulfillment_status == "reviewed":
        _append_unique(evidence, "manager_reviewed_fulfillment_timing")

    # Reviewed explanation is a separate, reproducible policy proof. Its money
    # never becomes a catalogue price, payment receipt or checkout capability.
    from management.services.approved_public_facts import (
        approved_public_facts, SUPPORTED_PUBLIC_FACT_LANGUAGES,
    )
    payment_policy = tuple((language, fact.version, hashlib.sha256(fact.public_text.encode()).hexdigest())
        for language in SUPPORTED_PUBLIC_FACT_LANGUAGES for fact in approved_public_facts(language)
        if fact.key == "payment_boundary" and fact.review_status.startswith("reviewed:"))
    fulfillment_policy = tuple((language, fact.version, hashlib.sha256(fact.public_text.encode()).hexdigest())
        for language in SUPPORTED_PUBLIC_FACT_LANGUAGES for fact in approved_public_facts(language)
        if fact.key == "dispatch" and fact.source_ref == "owner:2026-10-21:made-to-order"
        and fact.scope == "current_tshirts_and_hoodies_preparation_after_confirmed_payment"
        and fact.review_status == "reviewed:owner-brief:2026-10-21")

    return AuthorityReplyTruthContext(
        authorized_prices=tuple(prices),
        authorized_price_ranges=tuple(ranges),
        allowed_currency_codes=tuple(currencies),
        authorized_urls=authorized_urls,
        authorized_discount_amounts=tuple(discount_amounts),
        payment_confirmed=payment_confirmed,
        checkout_authority=actual_checkout,
        full_payment_confirmed=actual_checkout.fully_paid if actual_checkout is not None else legacy_full_payment if order is not None else None,
        shipping_authorities=shipping_authorities,
        order_created=order_created,
        shipment_state=shipment_state,
        known_tracking_refs=tracking_refs,
        approved_timing_claims=tuple(timing),
        timing_authorities=timing_authorities,
        fulfillment_deadline_authorities=fulfillment_deadline_authorities(_captured_fulfillment_projection(captured_client_state)),
        availability_authorities=fulfillment_availability_authorities(_captured_fulfillment_projection(captured_client_state)),
        fulfillment_review_status=fulfillment_status,
        explicitly_qualified_standard_dispatch_days=(
            ORDINARY_DISPATCH_WINDOW_DAYS if not fulfillment_status and fulfillment_policy else None
        ),
        allowed_sizes=tuple(sizes),
        allowed_fits=tuple(fits),
        allowed_colors=tuple(colors),
        authorized_actions=(),
        conversation_amounts=conversation_amounts,
        agreement_choices=agreement_choices,
        approved_payment_policy=payment_policy,
        approved_fulfillment_policy=fulfillment_policy,
        readiness_gaps=tuple(gaps),
        evidence_codes=tuple(evidence),
    )


__all__ = [
    "AuthorityReplyTruthContext",
    "EVIDENCE_CODES",
    "READINESS_GAP_CODES",
    "build_reply_truth_context",
    "fulfillment_timing_authorities",
    "fulfillment_deadline_authorities",
    "fulfillment_availability_authorities",
    "apply_fulfillment_timing_context",
    "CheckoutOrderAuthority",
    "capture_checkout_order_authority",
    "checkout_order_authority",
    "checkout_communication_prompt_guidance",
]
