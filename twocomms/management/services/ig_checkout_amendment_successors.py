"""A native hosted wish gets a new manager approval and immutable checkout.

Original chat rows are historical owner lineage. The amendment is a distinct
checkout-holder source, and the new hosted acknowledgement owns payment consent.
"""
from copy import deepcopy
from dataclasses import replace
import uuid

from django.conf import settings
from django.db import transaction
from django.utils.crypto import salted_hmac

from management.services import ig_assisted_commerce as commerce
from management.services import ig_checkout_cart_amendments as amendments

REQUEST_SOURCE_SCHEMA = "ig-checkout-amendment-review.v1"
CHOICE_SCHEMA = "ig-checkout-amendment-selected-scope.v1"
RESOLUTION_SCHEMA = "ig-checkout-cart-amendment-resolution.v1"


def _fail(reason):
    commerce._fail(reason)


def _native(amendment_id, *, now, allow_retired=False):
    try:
        return _native_source(amendment_id, now=now, allow_retired=allow_retired)
    except amendments.CheckoutAmendmentError as exc:
        _fail(exc.reason)


def _native_source(amendment_id, *, now, allow_retired=False):
    """Never trusts a caller capture, token/session dictionary or claimed owner."""
    from management.models import IgCheckoutCartAmendment
    receipt = IgCheckoutCartAmendment.objects.select_related(
        "selection_transition__session", "proposal__client", "proposal__deal", "proposal__commercial_episode").get(pk=commerce._uuid(amendment_id))
    body = amendments.verify_native_transition(receipt.selection_transition, now=now)
    cancelled = body.get("cancelled_scope") is True
    if not (body.get("pending_review") is True or cancelled and body.get("pending_review") is False):
        _fail("pending_amendment_required")
    proposal = receipt.proposal
    if proposal.status == "manager_review" and not cancelled:
        amendments.assert_pending_prebank(proposal, receipt)
    elif cancelled and proposal.status == "cancelled" and (not allow_retired or proposal.deal.active_checkout_proposal_id == proposal.pk):
        checked = amendments.verify_cancelled_amendment(receipt.pk, now=now)
        if not checked.get("ready"):
            _fail(checked.get("reason") or "native_cancelled_scope_unverified")
    elif allow_retired and proposal.status in ({"cancelled", "superseded"} if cancelled else {"superseded"}):
        execution = getattr(proposal, "assisted_commerce_execution", None)
        if (not execution or execution.active_slot is not None or execution.release_reason != ("cancelled" if cancelled else "superseded")
            or execution.order_id or execution.settled_at or not execution.released_at
            or proposal.payment_attempt_id or proposal.current_invoice_generation_id
            or proposal.winner_invoice_generation_id or proposal.invoice_generations.exists()
            or proposal.paid_at):
            _fail("amendment_origin_not_retired_prebank")
    else:
        _fail("pending_amendment_not_current")
    execution = getattr(proposal, "assisted_commerce_execution", None)
    if execution is None:
        _fail("assisted_amendment_origin_required")
    from management.services.ig_assisted_checkout import validate_assisted_execution_history
    original = validate_assisted_execution_history(execution, now=now)
    if (not original.get("ready") or body.get("original_execution_id") != str(execution.pk)
        or body.get("original_execution_digest") != execution.envelope_hmac):
        _fail("pending_original_execution_changed")
    current = amendments._native_capture(receipt, receipt.selection_transition, now=now)
    if (current.get("status") != "captured" or not current.get("coverage_complete")
        or current["fence"].get("native_amendment_id") != str(receipt.pk)
        or current["fence"].get("native_source_hmac") != receipt.source_hmac):
        _fail("pending_amendment_capture_changed")
    return receipt, body, current, execution


def _source_manifest(receipt, body, original):
    result = {"schema": REQUEST_SOURCE_SCHEMA, "amendment_id": str(receipt.pk),
        "source_hmac": receipt.source_hmac, "input_digest": receipt.input_digest,
        "origin_request_id": str(original.request_id), "origin_request_digest": original.request.envelope_hmac,
        "origin_decision_id": str(original.decision_id), "origin_decision_digest": original.decision.envelope_hmac,
        "origin_execution_id": str(original.pk), "origin_execution_digest": original.envelope_hmac,
        "origin_proposal_id": original.proposal_id, "origin_intention_id": str(original.intention_id),
        "origin_revision": receipt.from_revision, "origin_revision_digest": body["origin_revision_digest"],
        "requested_quote_lines": deepcopy(body["requested_quote_lines"]), "removed": deepcopy(body["removed"]),
        "source_authority": amendments.AUTHORITY, "payment_authority": False,
        "fresh_hosted_ack_required": True,
        "delivery_scope": (original.envelope["snapshot"].get("selection_evidence") or {}).get("delivery_scope", "now")}
    if body.get("cancelled_scope") is True:
        result.update(cancelled_scope=True, delivery_scope="later",
            cancelled_selected_line_ids=deepcopy(original.envelope["snapshot"]["selected_line_ids"]),
            remaining_deferred_line_ids=deepcopy(original.envelope["snapshot"]["deferred_line_ids"]))
    return result


def _wish_snapshot(receipt, body, current, original, *, now):
    """Only native quantity/removal edits; all old readiness/price is discarded."""
    from management.services.ig_assisted_scopes import _source_history
    _source_history(original.request)
    source = original.request.envelope["snapshot"]
    result = deepcopy(source)
    result.pop("ancestry", None)
    actual = {row["line_id"]: row for row in body["next_snapshot"]["lines"]}
    original_ids = {row["line_id"] for row in source["lines"]}
    if set(actual) - original_ids:
        _fail("amendment_added_unowned_line")
    if body.get("cancelled_scope") is True:
        from management.services.ig_assisted_scopes import request_line_map
        remaining = {row["current_line_id"] for row in request_line_map(original.request)
            if row["line_id"] in original.envelope["snapshot"]["deferred_line_ids"]}
        if set(actual) != remaining or not remaining:
            _fail("cancelled_amendment_requires_remaining_wait")
    wanted, specs = [], {}
    for line in source["lines"]:
        native = actual.get(line["line_id"])
        if native is None:
            continue
        row = deepcopy(line)
        quantity = native.get("quantity", 1)
        if type(quantity) is not int or not 1 <= quantity <= commerce.MAX_QUANTITY:
            _fail("amendment_quantity_invalid")
        row["original_index"] = len(wanted)
        row["source_choices"]["quantity"] = quantity
        row["source_choices"].pop("purchase_requested", None)
        row["source_evidence"].pop("purchase_requested", None)
        row["source_evidence"]["quantity"] = {"amendment_id": str(receipt.pk),
            "transition_id": receipt.selection_transition.pk, "source_digest": receipt.source_hmac,
            "source_kind": amendments.SCHEMA, "authority": amendments.AUTHORITY}
        if row.get("catalog_item_spec"):
            specs[row["line_id"]] = {key: deepcopy(value) for key, value in row["catalog_item_spec"].items()
                if key in {"product_id", "color_variant_id", "size", "fit_option_code", "option_values"}}
            specs[row["line_id"]]["qty"] = quantity
        wanted.append(row)
    if not wanted:
        _fail("empty_amendment_basket")
    if sum(row["source_choices"]["quantity"] for row in wanted) > commerce.MAX_QUANTITY:
        _fail("amendment_quantity_bound")
    observations = commerce._canonical_catalog_observations(specs)
    for row in wanted:
        row["catalog_item_spec"] = deepcopy(observations.get(row["line_id"]))
    settings_obj, permission = commerce._permission(receipt.client, source["settings_id"])
    result.update(lines=wanted, questions=[deepcopy(row) for row in source["questions"]
        if not row.get("line_id") or row["line_id"] in actual], full_capture=deepcopy(current),
        source_head=deepcopy(current["source_watermark"]),
        owner_scope=commerce._business_owner(receipt.client, current["scope"]),
        settings_permission_epoch=permission.settings_epoch, client_permission_epoch=permission.client_epoch,
        expires_at=min(now + commerce.REQUEST_TTL, receipt.proposal.expires_at).isoformat(),
        checkout_amendment=_source_manifest(receipt, body, original))
    return result


def create_amendment_review_request(amendment_id, *, operation_id, now=None):
    """Append a wish-only Request; the existing operator edits/approves it."""
    from management.models import IgClient, IgFollowUpTask
    from management.services.ig_commercial_episodes import commercial_episode_client_lock
    Request = commerce._models()[0]
    now, operation = commerce._now(now), commerce._uuid(operation_id)
    receipt, body, current, original = _native(amendment_id, now=now)
    with commercial_episode_client_lock(receipt.client_id), transaction.atomic():
        client = IgClient.objects.select_for_update().get(pk=receipt.client_id)
        receipt, body, current, original = _native(amendment_id, now=now)
        input_digest = commerce._digest({"amendment_id": str(receipt.pk), "source_hmac": receipt.source_hmac,
            "operation_id": str(operation), "client_id": client.pk})
        prior = Request.objects.filter(operation_id=operation).first()
        if prior:
            if (not commerce._verified(prior, "request") or prior.client_id != client.pk
                or prior.envelope.get("operation_input_digest") != input_digest):
                _fail("operation_conflict")
            _data, task, _current = verify_amendment_request(prior, client=client, now=now)
            _queue_manager_review(receipt, body, original, prior, task)
            return prior, False
        duplicate = Request.objects.filter(client=client,
            envelope__snapshot__checkout_amendment__amendment_id=str(receipt.pk)).first()
        if duplicate:
            _data, task, _current = verify_amendment_request(duplicate, client=client, now=now)
            _queue_manager_review(receipt, body, original, duplicate, task)
            return duplicate, False
        snapshot = _wish_snapshot(receipt, body, current, original, now=now)
        task, _ = IgFollowUpTask.objects.get_or_create(event_key="checkout-amendment-review:" + str(receipt.pk),
            defaults={"client": client, "deal": receipt.proposal.deal, "kind": "manager_task", "due_at": now,
                "reason": "checkout_amendment_review", "manager_approval_status": "pending",
                "manager_approval_requested_at": now, "message_text": "Клієнт змінив кількість або склад кошика. Перевірте залишок, ціну й умови перед новим оформленням."})
        if task.client_id != client.pk or task.kind != "manager_task":
            _fail("manager_case_owner_changed")
        request = commerce._seal(Request(intention_id=uuid.uuid5(uuid.NAMESPACE_URL,
            "twocomms:checkout-amendment:" + str(receipt.pk)), version=1, operation_id=operation,
            client=client, task=task, source_id=original.request.source_id, revision_id=original.request.revision_id,
            episode_id=receipt.proposal.commercial_episode_id, deal_id=receipt.proposal.deal_id,
            source_namespace=original.request.source_namespace, reset_floor=current["scope"]["reset_floor"],
            client_permission_epoch=client.reply_permission_epoch, full_capture_digest=current["capture_digest"]),
            "request", snapshot, operation_input_digest=input_digest)
        commerce._publish_task_head(task, request)
        _queue_manager_review(receipt, body, original, request, task)
        return request, True


def _queue_manager_review(receipt, body, original, request, task):
    """Commit the existing durable alert alongside the native review request."""
    from management.services.ig_alerts import client_admin_url, format_alert
    from management.services.instagram_bot import notify_manager
    approved = {row["line_id"]: row for row in original.decision.envelope["snapshot"]["items"]}
    lines = [f"Завдання: {task.pk}", "Кошик змінено на сторінці оформлення; це запит, а не повідомлення Instagram."]
    for row in body["requested_quote_lines"]:
        prior = approved.get(row["line_id"], {})
        title = " ".join(str(prior.get("title") or row.get("title") or "Товар").split())[:160]
        lines.append(f"Запит: {title}, кількість {row['quantity']} (раніше {prior.get('quantity', '?')}).")
    for row in body["removed"]:
        if row.get("disposition") != "cancelled_by_checkout_holder":
            continue
        prior = approved.get(row["line_id"], {})
        title = " ".join(str(prior.get("title") or "Товар").split())[:160]
        lines.append(f"Вилучено з оформлення: {title}, кількість {row['quantity']}.")
    if body.get("cancelled_scope") is True:
        for row in request.envelope["snapshot"]["lines"]:
            prior = approved.get(row["line_id"], {})
            title = " ".join(str(prior.get("title") or "Товар").split())[:160]
            lines.append(f"Збережено для пізнішого оформлення: {title}, кількість {row['source_choices']['quantity']}. Попередній WAIT не підтверджує готовність.")
    lines.append("Заново перевірте точну наявність, ціну та доставку. Оплата заблокована до нового погодження; нова сума ще не затверджена.")
    if not notify_manager(format_alert("IG: клієнт змінив кошик — потрібне рішення менеджера", lines=lines,
            url=client_admin_url(receipt.client_id), url_label="Відкрити заявку в CRM:"),
            dedupe_key="checkout-amendment-review:" + str(receipt.pk), event_type="manager_task_ready",
            client=receipt.client, metadata={"requires_human_review": True, "followup_task_id": task.pk,
                "assisted_request_id": str(request.pk), "checkout_amendment_id": str(receipt.pk)},
            deliver_immediately=False, raise_on_error=True):
        _fail("manager_review_notification_unavailable")


def verify_amendment_request(request, client, now=None):
    """Current source admission independent of the historical chat head."""
    now = commerce._now(now)
    data = request.envelope["snapshot"]
    manifest = data.get("checkout_amendment") or {}
    if (not commerce._verified(request, "request") or request.client_id != client.pk
        or manifest.get("schema") != REQUEST_SOURCE_SCHEMA or now >= commerce._date(data["expires_at"])
        or request.created_at > now):
        _fail("amendment_request_unverified")
    receipt, body, current, original = _native(manifest["amendment_id"], now=now, allow_retired=True)
    if (receipt.client_id != client.pk or manifest != _source_manifest(receipt, body, original)
        or request.intention_id == original.intention_id
        or request.source_id != original.request.source_id or request.revision_id != original.request.revision_id
        or data["sources"] != original.request.envelope["snapshot"]["sources"]
        or commerce._configuration_capture(current) != commerce._configuration_capture(data["full_capture"])):
        _fail("amendment_request_source_changed")
    commerce._permission(client, data["settings_id"], expected_settings_epoch=data["settings_permission_epoch"],
        expected_client_epoch=request.client_permission_epoch)
    from management.services.ig_assisted_scopes import _source_history
    _source_history(original.request)
    task = commerce._task(client, request.task_id)
    if task:
        head = (task.manager_context or {}).get("assisted_commerce") or {}
        if head.get("request_id") != str(request.pk) or head.get("request_digest") != request.envelope_hmac:
            _fail("manager_case_head_changed")
    return data, task, current


def validate_native_payment_capture(execution, *, now=None):
    """Fresh native wishlist/configuration authority, independent of neutral FAQ."""
    now = commerce._now(now)
    body = execution.envelope["snapshot"]
    if (not commerce._verified(execution, "execution") or execution.scope != "partial"
        or body.get("choice_proof", {}).get("schema") != CHOICE_SCHEMA
        or execution.request_id != execution.decision.request_id
        or execution.client_id != execution.request.client_id):
        _fail("amendment_payment_capture_unverified")
    _data, _task, current = verify_amendment_request(execution.request, execution.client, now)
    expected = body.get("current_quote_capture") or {}
    manifest = execution.request.envelope["snapshot"].get("checkout_amendment") or {}
    if (commerce._configuration_capture(current) != commerce._configuration_capture(expected)
        or current.get("fence", {}).get("native_amendment_id") != manifest.get("amendment_id")
        or current.get("fence", {}).get("native_source_hmac") != manifest.get("source_hmac")):
        _fail("amendment_payment_capture_changed")
    return current


def image_request_owners(request):
    """A sealed amendment may reuse only its exact predecessor's owned photo."""
    if not commerce._verified(request, "request"):
        _fail("image_request_unverified")
    from management.models import IgCheckoutCartAmendment
    owners, current = {request.pk}, request
    for depth in range(33):
        manifest = current.envelope["snapshot"].get("checkout_amendment")
        if not manifest:
            return owners
        if depth == 32:
            _fail("image_amendment_history_bound")
        receipt = IgCheckoutCartAmendment.objects.select_related("selection_transition__session",
            "proposal__assisted_commerce_execution__request", "proposal__assisted_commerce_execution__decision").get(
                pk=commerce._uuid(manifest["amendment_id"]), client_id=request.client_id)
        body = amendments.verify_native_transition(receipt.selection_transition, historical=True)
        original = receipt.proposal.assisted_commerce_execution
        if (not commerce._verified(original, "execution") or original.client_id != request.client_id
            or manifest != _source_manifest(receipt, body, original)
            or not commerce._verified(original.request, "request") or original.request_id in owners):
            _fail("image_amendment_owner_changed")
        owners.add(original.request_id)
        current = original.request
    _fail("image_amendment_history_bound")


def native_cancelled_scope(execution, line_ids):
    """Only a real holder's remove-all fact cancels the old selected units."""
    from management.models import IgCheckoutCartAmendment
    if (execution.release_reason != "cancelled" or execution.active_slot is not None
        or execution.order_id or execution.settled_at or not execution.released_at
        or execution.proposal.status != "cancelled" or execution.proposal.payment_attempt_id
        or execution.proposal.invoice_generations.exists() or execution.proposal.winner_invoice_generation_id):
        return False
    receipt = IgCheckoutCartAmendment.objects.filter(proposal_id=execution.proposal_id,
        client_id=execution.client_id, source_body__cancelled_scope=True).order_by("-created_at", "pk").first()
    if receipt is None:
        return False
    body = amendments.verify_native_transition(receipt.selection_transition, historical=True)
    removed = {row.get("line_id") for row in body.get("removed", [])
        if row.get("disposition") == "cancelled_by_checkout_holder"}
    return bool(body.get("cancelled_scope") is True and body.get("operation") == "remove" and body.get("requested_quote_lines") == []
        and body.get("original_execution_id") == str(execution.pk)
        and body.get("original_execution_digest") == execution.envelope_hmac
        and set(line_ids) <= removed and execution.proposal.revision == receipt.from_revision)


def verified_amendment_selection(request, decision, *, actor, now=None, historical=False, execution=None):
    """Wishlist preparation authority; bank consent is the fresh hosted ack."""
    from management.services import ig_assisted_scopes as scopes
    now = commerce._now(now)
    if historical:
        cap = commerce.verified_assisted_decision_from_evidence(request, decision, historical=True)
        from management.models import IgCheckoutCartAmendment
        receipt = IgCheckoutCartAmendment.objects.select_related("selection_transition__session", "proposal__assisted_commerce_execution").get(
            pk=request.envelope["snapshot"]["checkout_amendment"]["amendment_id"])
        body = amendments.verify_native_transition(receipt.selection_transition, historical=True)
        original = receipt.proposal.assisted_commerce_execution
        if request.envelope["snapshot"]["checkout_amendment"] != _source_manifest(receipt, body, original):
            _fail("amendment_history_changed")
        current = deepcopy(execution.envelope["snapshot"]["current_quote_capture"])
        coverage = deepcopy(execution.envelope["snapshot"]["coverage"])
    elif execution is not None:
        cap = commerce.verified_assisted_decision_from_evidence(request, decision, historical=False)
        verify_amendment_request(request, client=execution.client, now=now)
        receipt, body, current, original = _native(request.envelope["snapshot"]["checkout_amendment"]["amendment_id"],
            now=now, allow_retired=True)
        coverage = deepcopy(execution.envelope["snapshot"]["coverage"])
    else:
        cap = commerce.load_verified_assisted_decision(request.pk, decision.pk, actor=actor,
            expected_request_digest=request.envelope_hmac, expected_decision_digest=decision.envelope_hmac, now=now)
        receipt, body, current, original = _native(request.envelope["snapshot"]["checkout_amendment"]["amendment_id"],
            now=now, allow_retired=True)
        coverage = scopes.intention_coverage(request.client_id, request.intention_id, now=now,
            exclude_execution_id=getattr(execution, "pk", None))
        if not coverage.get("ready"):
            _fail(coverage.get("reason") or "amendment_coverage_changed")
    quote, lines = scopes._quote_lines(request, decision, parent_lines=cap.lines)
    if not historical and execution is None:
        coverage = scopes._selected_coverage(coverage, quote)
    old_body = original.envelope["snapshot"]
    purchase = deepcopy(old_body["purchase_proof"])
    source_id = original.choice_source_id or original.purchase_source_id
    choice = {"schema": CHOICE_SCHEMA, "amendment_id": str(receipt.pk), "source_hmac": receipt.source_hmac,
        "client_id": request.client_id, "request_id": str(request.pk), "request_digest": request.envelope_hmac,
        "decision_id": str(decision.pk), "decision_digest": decision.envelope_hmac, "terms_digest": quote["terms_digest"],
        "source_id": source_id, "source_authority": amendments.AUTHORITY,
        "historical_ig_source_only": True, "fresh_hosted_ack_required": True}
    return scopes.VerifiedAssistedSelection(cap, lines, commerce._json(quote), commerce._json(coverage),
        commerce._json(choice), commerce._json(purchase), commerce._json(request.envelope["snapshot"]["full_capture"]),
        commerce._json(current), request.pk, request.envelope_hmac, request.full_capture_digest,
        getattr(execution, "pk", None), "historical" if historical else "execution" if execution else "unconsumed", scopes._ISSUER)


def prepare_amendment_successor(client_id, *, actor, request_id, decision_id, expected_request_digest,
        expected_decision_digest, operation_id, now=None):
    from management.models import (IgClient, IgCheckoutCartAmendmentResolution as Resolution,
        IgCheckoutAccessToken)
    from management.services.ig_commercial_episodes import commercial_episode_client_lock
    from management.services import ig_assisted_checkout as checkout
    Request, Decision, Execution, _Image = commerce._models()
    now = commerce._now(now)
    with commercial_episode_client_lock(client_id), transaction.atomic():
        client = IgClient.objects.select_for_update().get(pk=client_id)
        operator = commerce._actor(getattr(actor, "pk", None))
        request = Request.objects.select_for_update().get(pk=commerce._uuid(request_id), client=client)
        decision = Decision.objects.select_for_update().get(pk=commerce._uuid(decision_id), request=request, client=client)
        if request.envelope_hmac != expected_request_digest or decision.envelope_hmac != expected_decision_digest:
            _fail("approved_decision_changed")
        manifest = request.envelope["snapshot"].get("checkout_amendment") or {}
        prior = Resolution.objects.select_related("successor_proposal", "execution").filter(amendment_id=manifest.get("amendment_id")).first()
        if prior:
            if (prior.request_id != request.pk or prior.decision_id != decision.pk
                or str(prior.execution.operation_id) != str(operation_id)):
                _fail("operation_conflict")
            _verified_resolution(prior, now=now)
            raw = checkout._access_token(prior.execution, now=now)
            return {"ready": True, "reason": "", "proposal": prior.successor_proposal,
                "proposal_id": prior.successor_proposal_id, "public_id": str(prior.successor_proposal.public_id),
                "checkout_url": checkout._url(prior.successor_proposal, raw), "replayed": True}
        cap = verified_amendment_selection(request, decision, actor=operator, now=now)
        result = checkout.prepare_assisted_checkout(client.pk, actor=operator, request_id=request.pk,
            decision_id=decision.pk, expected_request_digest=expected_request_digest,
            expected_decision_digest=expected_decision_digest, operation_id=operation_id, verified_selection=cap)
        if not result.get("ready"):
            _fail(result.get("reason") or "amendment_successor_unavailable")
        execution = Execution.objects.get(proposal_id=result["proposal_id"], request=request, decision=decision)
        from management.models import IgCheckoutCartAmendment
        receipt = IgCheckoutCartAmendment.objects.get(pk=manifest["amendment_id"], client=client)
        raw = checkout._access_token(execution, now=now)
        child = IgCheckoutAccessToken.objects.get(proposal=execution.proposal,
            token_digest=IgCheckoutAccessToken.digest(raw), kind="bot")
        body = {"schema": RESOLUTION_SCHEMA, "amendment_id": str(receipt.pk), "source_hmac": receipt.source_hmac,
            "input_digest": receipt.input_digest, "client_id": client.pk,
            "origin_proposal_id": receipt.proposal_id, "origin_revision": receipt.from_revision,
            "origin_revision_digest": receipt.source_body["origin_revision_digest"],
            "successor_proposal_id": execution.proposal_id, "successor_public_id": str(execution.proposal.public_id),
            "successor_revision": execution.proposal.revision, "successor_items_digest": execution.proposal.items_digest,
            "request_id": str(request.pk), "request_digest": request.envelope_hmac,
            "decision_id": str(decision.pk), "decision_digest": decision.envelope_hmac,
            "execution_id": str(execution.pk), "execution_digest": execution.envelope_hmac,
            "root_token_id": receipt.token_id, "child_token_id": child.pk, "actor_id": operator.pk,
            "approved_at": now.isoformat(), "fresh_hosted_ack_required": True}
        Resolution.objects.create(amendment=receipt, client=client, origin_proposal=receipt.proposal,
            successor_proposal=execution.proposal, request=request, decision=decision, execution=execution,
            actor=operator, root_token=receipt.token, child_token=child, body=body,
            body_hmac=amendments._sign("resolution", body))
        return result


def _verified_resolution(resolution, *, now):
    body = resolution.body
    if (not amendments._valid_signature("resolution", body, resolution.body_hmac)
        or body.get("schema") != RESOLUTION_SCHEMA or resolution.created_at > now
        or body.get("amendment_id") != str(resolution.amendment_id)
        or body.get("client_id") != resolution.client_id
        or body.get("origin_proposal_id") != resolution.origin_proposal_id
        or body.get("successor_proposal_id") != resolution.successor_proposal_id
        or body.get("root_token_id") != resolution.root_token_id
        or body.get("child_token_id") != resolution.child_token_id
        or body.get("request_id") != str(resolution.request_id)
        or body.get("decision_id") != str(resolution.decision_id)
        or body.get("execution_id") != str(resolution.execution_id)):
        _fail("amendment_resolution_unverified")
    manifest = resolution.request.envelope["snapshot"].get("checkout_amendment") or {}
    if (body.get("actor_id") != resolution.actor_id or body.get("source_hmac") != resolution.amendment.source_hmac
        or body.get("input_digest") != resolution.amendment.input_digest
        or manifest.get("amendment_id") != str(resolution.amendment_id)
        or manifest.get("source_hmac") != resolution.amendment.source_hmac
        or resolution.amendment.proposal_id != resolution.origin_proposal_id
        or resolution.amendment.client_id != resolution.client_id
        or resolution.amendment.token_id != resolution.root_token_id
        or resolution.successor_proposal.client_id != resolution.client_id
        or resolution.request.client_id != resolution.client_id or resolution.decision.client_id != resolution.client_id
        or body.get("fresh_hosted_ack_required") is not True):
        _fail("amendment_resolution_owner_changed")
    from management.services.ig_assisted_checkout import validate_assisted_execution_history
    verified = validate_assisted_execution_history(resolution.execution, now=now)
    if (not verified.get("ready") or resolution.execution.proposal_id != resolution.successor_proposal_id
        or resolution.execution.request_id != resolution.request_id or resolution.execution.decision_id != resolution.decision_id
        or resolution.execution.client_id != resolution.client_id
        or resolution.request.envelope_hmac != body.get("request_digest")
        or resolution.decision.envelope_hmac != body.get("decision_digest")
        or resolution.execution.envelope_hmac != body.get("execution_digest")
        or resolution.successor_proposal.items_digest != body.get("successor_items_digest")
        or str(resolution.successor_proposal.public_id) != body.get("successor_public_id")
        or resolution.successor_proposal.revision != body.get("successor_revision")):
        _fail("amendment_resolution_changed")
    amendments.verify_native_transition(resolution.amendment.selection_transition, historical=True, now=now)
    return body


def resolve_amendment_successor(proposal, token, *, now=None):
    """Resolve at most 32 immutable edges; every grant retains its scope/TTL."""
    from management.models import IgClient, IgCheckoutAccessToken, IgFunnelResetAudit, IgCheckoutCartAmendmentResolution as Resolution
    from management.services.ig_commercial_episodes import commercial_episode_client_lock
    now = commerce._now(now)
    with commercial_episode_client_lock(proposal.client_id), transaction.atomic():
        client = IgClient.objects.select_for_update().get(pk=proposal.client_id)
        actual_token = IgCheckoutAccessToken.objects.select_for_update().filter(pk=token.pk, proposal_id=proposal.pk,
            revoked_at__isnull=True, expires_at__gt=now).first()
        if not actual_token or client.hidden_at or client.privacy_erasure_started_at:
            _fail("amendment_root_grant_unavailable")
        if actual_token.kind not in {"share", "bot", "manager"}:
            _fail("amendment_root_grant_scope_changed")
        current, grant = proposal, actual_token
        deadline = min(actual_token.expires_at, proposal.expires_at)
        reset_id = IgFunnelResetAudit.objects.filter(client=client).order_by("-pk").values_list("pk", flat=True).first()
        seen, chain = {proposal.pk}, []
        for depth in range(33):
            rows = list(Resolution.objects.select_related("amendment__selection_transition__session", "request", "decision",
                "execution", "successor_proposal", "child_token").filter(origin_proposal_id=current.pk, client=client)[:2])
            if len(rows) > 1:
                _fail("amendment_resolution_ambiguous")
            if not rows:
                if not chain:
                    return {"ready": False, "reason": "manager_reapproval_required"}
                return {"ready": True, "proposal": current, "token": grant,
                    "proof": {**chain[-1], "chain": chain, "fresh_hosted_ack_required": True,
                        "pending_review": current.status == "manager_review"}}
            if depth == 32:
                _fail("amendment_resolution_history_bound")
            resolution = rows[0]
            body = _verified_resolution(resolution, now=now)
            child_proposal = resolution.successor_proposal
            if child_proposal.pk in seen or current.client_id != client.pk:
                _fail("amendment_resolution_cycle")
            data = resolution.request.envelope["snapshot"]
            current_settings, _permission = commerce._permission(client, data["settings_id"], expected_settings_epoch=data["settings_permission_epoch"],
                expected_client_epoch=resolution.request.client_permission_epoch)
            from management.services.instagram_bot import ingress_provider_namespace
            if (ingress_provider_namespace(current_settings) != resolution.request.source_namespace
                or data["full_capture"]["scope"].get("reset_id") != reset_id
                or child_proposal.status != "paid" and client.current_commercial_episode_id != resolution.execution.episode_id):
                _fail("amendment_descendant_owner_changed")
            child = IgCheckoutAccessToken.objects.select_for_update().filter(pk=resolution.child_token_id,
                proposal=child_proposal, revoked_at__isnull=True, expires_at__gt=now).first()
            if not child or child.kind not in {"bot", "manager"}:
                _fail("amendment_child_grant_unavailable")
            deadline = min(deadline, child.expires_at, child_proposal.expires_at)
            if deadline <= now:
                _fail("amendment_child_grant_unavailable")
            # Any real origin edit grant already has these capabilities. The
            # approval's exact root token remains sealed in the native edge;
            # a different owned BOT token may follow that approved edge.
            desired_kind = "share" if actual_token.kind == "share" else child.kind
            if desired_kind == "share" or child.expires_at > deadline:
                raw = salted_hmac("ig-checkout-amendment-delegation.v1",
                    f"{actual_token.pk}:{resolution.pk}:{desired_kind}", algorithm="sha256").hexdigest()
                child, _ = IgCheckoutAccessToken.objects.get_or_create(token_digest=IgCheckoutAccessToken.digest(raw),
                    defaults={"proposal": child_proposal, "kind": desired_kind, "expires_at": deadline})
                if (child.kind != desired_kind or child.proposal_id != child_proposal.pk or child.revoked_at
                    or child.expires_at <= now or child.expires_at > deadline):
                    _fail("amendment_delegated_grant_changed")
            if actual_token.kind == "share" and child.kind != "share":
                _fail("amendment_shared_grant_changed")
            seen.add(child_proposal.pk)
            chain.append({"resolution_id": str(resolution.pk), "body_hmac": resolution.body_hmac,
                "amendment_id": body["amendment_id"]})
            current, grant = child_proposal, child
