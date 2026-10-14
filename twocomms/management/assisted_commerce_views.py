"""Current authenticated operator APIs; source facts come from native producers."""
import json
import uuid
from urllib.parse import urljoin, urlsplit

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404, HttpResponse, JsonResponse
from django.urls import path
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_GET, require_http_methods

from management.services import ig_assisted_commerce as commerce
from management.services import ig_assisted_commerce_media as media

CAS_KEYS = {"expected_request_version", "expected_request_digest", "expected_full_capture_digest",
    "expected_decision_version", "expected_draft_digest"}


def _error(reason, status=409):
    return JsonResponse({"success": False, "reason": reason}, status=status)


def _owner(request, client_id):
    from management.models import IgClient
    commerce._actor(getattr(request.user, "pk", None))
    client = IgClient.objects.filter(pk=client_id).first()
    if not client:
        raise Http404
    if client.privacy_erasure_started_at:
        commerce._fail("client_erasing")
    if client.hidden_at:
        commerce._fail("client_unavailable")
    return client


def _source_cas(client):
    from management.models import IgCustomerTurnRevision, InstagramBotSettings
    from management.services.ig_commercial_episodes import commercial_episode_client_lock
    revision = IgCustomerTurnRevision.objects.filter(client=client).order_by("-pk").first()
    settings_obj = InstagramBotSettings.objects.order_by("pk").first()
    if not revision or not settings_obj:
        return {"ready": False, "reason": "source_not_selected"}
    try:
        with commercial_episode_client_lock(client.pk), transaction.atomic():
            capture = commerce._capture(client, revision.pk, settings_obj.pk, timezone.now(),
                expected_revision_digest=revision.snapshot_digest)
        return {"ready": True, "revision_id": revision.pk,
            "expected_revision_digest": revision.snapshot_digest,
            "expected_full_capture_digest": capture["full_capture"]["capture_digest"]}
    except commerce.AssistedDecisionError as exc:
        return {"ready": False, "reason": exc.reason}


def _catalog_image_controls(value):
    """Public catalog thumbnails from the signed source's exact native variant."""
    from django.db.models import Prefetch
    from productcolors.models import ProductColorVariant, ProductColorImage
    from storefront.models import ProductStatus

    source = {line["line_id"]: line.get("catalog_item_spec") or {}
        for line in (value.get("request") or {}).get("lines", [])}
    catalog = {}
    for item in value.get("editable_items", []):
        spec = source.get(item["line_id"], {})
        if (item.get("item_kind") == "catalog_apparel" and spec
                and item.get("product_id") == spec.get("product_id")
                and item.get("color_variant_id") == spec.get("color_variant_id")):
            catalog[item["line_id"]] = spec
    if not catalog:
        return
    variants = {variant.pk: variant for variant in ProductColorVariant.objects.filter(
        pk__in={spec["color_variant_id"] for spec in catalog.values()},
        product__status=ProductStatus.PUBLISHED).select_related("product").prefetch_related(
            Prefetch("images", queryset=ProductColorImage.objects.order_by("order", "id"),
                to_attr="assisted_catalog_images"))}
    for control in value.get("item_controls", []):
        spec = catalog.get(control["line_id"])
        variant = variants.get(spec["color_variant_id"]) if spec else None
        if not variant or variant.product_id != spec["product_id"]:
            continue
        photos = variant.assisted_catalog_images
        image = photos[0].image if photos else variant.product.main_image
        if not image:
            continue
        try:
            image_url = urljoin(settings.SITE_BASE_URL.rstrip("/") + "/", image.url)
        except (AttributeError, ValueError):
            continue
        parsed = urlsplit(image_url)
        if parsed.scheme in {"https", "http"} and parsed.netloc and not parsed.username and not parsed.password:
            control["catalog_image_url"] = image_url


def _context(client, actor, request_id=None):
    value = commerce.operator_context(client.pk, actor=actor, request_id=request_id)
    _catalog_image_controls(value)
    value["source_cas"] = _source_cas(client)
    from management.services.ig_assisted_checkout import current_assisted_offer, validate_assisted_proposal
    from management.ig_assisted_commerce_models import IgAssistedCommerceExecution as Execution
    offer = current_assisted_offer(client)
    matched = bool(value.get("request") and offer.get("request")
        and str(offer["request"].pk) == value["request"]["id"])
    reason = offer.get("prepare_reason") or offer.get("reason") or value.get("can_prepare_reason") or "approved_decision_missing"
    value["can_prepare_checkout"] = matched and offer.get("can_prepare_checkout") is True
    value["can_prepare_reason"] = reason
    labels = {
        "approved_decision_missing": "Спочатку збережіть і затвердьте точні умови товарів.",
        "assisted_request_absent": "Спочатку підготуйте перевірку поточного кошика.",
        "checkout_purchase_authority_missing": "Клієнт ще має підтвердити оформлення саме цих позицій. Затвердження менеджера зберігає умови товарів.",
        "customer_purchase_required": "Клієнт ще має підтвердити оформлення саме цих позицій. Затвердження менеджера зберігає умови товарів.",
        "assisted_full_ready_required": "Для оформлення цього кошика потрібно підтвердити фізичну готовність усіх позицій.",
        "full_ready_required": "Для оформлення цього кошика потрібно підтвердити фізичну готовність усіх позицій.",
        "assisted_checkout_already_prepared": "Оформлення вже підготовлено. Перегляньте наявну пропозицію; нова не створюється.",
    }
    native_message = offer.get("prepare_message") if matched else None
    value["prepare_reason"] = "" if value["can_prepare_checkout"] else (native_message or labels.get(reason, "Оформлення зараз недоступне. Звірте поточний запит, затверджені умови та підтвердження клієнта."))
    if value.get("request"):
        execution = Execution.objects.select_related("request", "decision", "proposal").filter(
            request_id=value["request"]["id"], client=client).first()
        if execution:
            checked = validate_assisted_proposal(execution.proposal, fresh=False)
            if checked.get("ready") is True:
                value.update(status="prepared", reason="", can_prepare=False, can_prepare_checkout=False,
                    can_approve=False, approved_head=commerce._decision_dto(execution.decision))
                initiator = execution.envelope["snapshot"].get("initiator") or {}
                value["prepared_checkout"] = {"ready": True, "proposal_id": execution.proposal_id,
                    "public_id": str(execution.proposal.public_id), "state": execution.proposal.status,
                    "operation_id": str(execution.operation_id),
                    "request_id": str(execution.request_id), "decision_id": str(execution.decision_id),
                    "expected_request_digest": execution.request_digest,
                    "expected_decision_digest": execution.decision_digest,
                    "can_recover_checkout": initiator.get("kind") == "manager" and initiator.get("actor_id") == actor.pk}
                if execution.scope == "partial":
                    snapshot = execution.envelope["snapshot"]
                    evidence, choice = snapshot["selection_evidence"], snapshot["choice_proof"]
                    value["prepared_checkout"].update(action="prepare_selected", task_id=choice["task_id"],
                        expected_choice_digest=commerce._digest(choice), expected_terms_digest=evidence["terms_digest"],
                        expected_coverage_digest=evidence["ledger_coverage_digest"])
                value["prepare_reason"] = labels["assisted_checkout_already_prepared"]
    from management.services.ig_assisted_stock_sync import projection as stock_projection
    value["stock_sync"] = stock_projection(client.pk, actor=actor,
        request_id=(value.get("request") or {}).get("id"),
        decision_id=(value.get("decision_head") or {}).get("id"))
    if value["stock_sync"].get("requires_sync") is True:
        value["can_prepare_checkout"] = False
        if not native_message:
            value["can_prepare_reason"] = "assisted_stock_recount_required"
            value["prepare_reason"] = "Спочатку звірте фактичний залишок позицій каталогу. Перевірена кількість для клієнта не є загальним складським залишком."
    from management.services.ig_assisted_partial import partial_intention_projection
    value["partial_intention"] = partial_intention_projection(client.pk, actor=actor,
        request_id=(value.get("request") or {}).get("id"))
    return value


def _checkout_payload(result):
    """No ORM objects, internal capabilities or raw token in operator JSON."""
    payload = {key: result[key] for key in ("ready", "reason", "proposal_id", "public_id", "checkout_url", "replayed") if key in result}
    if result.get("proposal") is not None:
        payload["state"] = result["proposal"].status
    return payload


def _payload(request):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate fields")
            result[key] = value
        return result
    if len(request.body) > commerce.MAX_JSON_BYTES:
        raise ValueError("body bound")
    value = json.loads(request.body, object_pairs_hook=unique,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
    if not isinstance(value, dict):
        raise ValueError("object required")
    return value


def _uuid(value):
    if not isinstance(value, str) or len(value) != 36:
        raise ValueError("UUID required")
    return uuid.UUID(value)


def _request_owned(client, request_id):
    from management.ig_assisted_commerce_models import IgAssistedCommerceRequest
    if not IgAssistedCommerceRequest.objects.filter(pk=request_id, client=client).exists():
        raise Http404


@login_required(login_url="management_login")
@require_http_methods(["GET", "POST"])
@never_cache
@csrf_protect
def assisted_commerce_api(request, client_id):
    try:
        client = _owner(request, client_id)
        if request.method == "GET":
            selected = _uuid(request.GET["request_id"]) if request.GET.get("request_id") else None
            if selected:
                _request_owned(client, selected)
            return JsonResponse({"success": True, "commerce": _context(client, request.user, selected)})
        if request.content_type == "multipart/form-data":
            allowed = {"action", "operation_id", "request_id", *CAS_KEYS}
            if (set(request.POST) != allowed or any(len(request.POST.getlist(key)) != 1 for key in allowed)
                or request.POST["action"] != "upload" or set(request.FILES) != {"image"}
                or len(request.FILES.getlist("image")) != 1):
                raise ValueError("upload fields invalid")
            payload = request.POST.dict()
            payload["expected_request_version"] = int(payload["expected_request_version"])
            payload["expected_decision_version"] = int(payload["expected_decision_version"])
            request_id = _uuid(payload["request_id"])
            _request_owned(client, request_id)
            image, created = media.upload_assisted_image(request_id, actor=request.user,
                operation_id=_uuid(payload["operation_id"]), upload=request.FILES["image"],
                **{key: payload[key] for key in CAS_KEYS})
            return JsonResponse({"success": True, "created": created, "image": media.image_payload(image),
                "commerce": _context(client, request.user, request_id)})
        if request.content_type != "application/json":
            raise ValueError("JSON required")
        payload = _payload(request)
        action = payload.get("action")
        operation = _uuid(payload.get("operation_id"))
        if action == "request":
            required = {"action", "operation_id", "revision_id", "expected_revision_digest", "expected_full_capture_digest"}
            optional = {"intention_id", "expected_request_version", "expected_request_digest", "task_id"}
            if set(payload) - required - optional or not required <= set(payload):
                raise ValueError("request fields invalid")
            native = _source_cas(client)
            if not native["ready"]:
                return _error(native["reason"])
            if any(payload[key] != native[key] or type(payload[key]) is not type(native[key])
                for key in ("revision_id", "expected_revision_digest", "expected_full_capture_digest")):
                return _error("source_capture_changed")
            from management.models import InstagramBotSettings
            settings_obj = InstagramBotSettings.objects.order_by("pk").first()
            commerce._actor(request.user.pk)
            row, created = commerce.create_assisted_request(native["revision_id"], settings_id=settings_obj.pk,
                operation_id=operation, expected_revision_digest=native["expected_revision_digest"],
                expected_full_capture_digest=native["expected_full_capture_digest"],
                **{key: payload[key] for key in optional if key in payload})
            return JsonResponse({"success": True, "created": created, "request_id": str(row.pk),
                "commerce": _context(client, request.user, row.pk)})
        if action in {"draft", "approve", "reject"}:
            extra = {"items"} if action == "draft" else {"rejection_reason"} if action == "reject" else set()
            if action == "draft" and "shipping_terms" in payload:
                extra.add("shipping_terms")
            if set(payload) != {"action", "operation_id", "request_id", *CAS_KEYS, *extra}:
                raise ValueError("decision fields invalid")
            request_id = _uuid(payload["request_id"])
            _request_owned(client, request_id)
            decision, created = commerce.record_assisted_decision(request_id, actor=request.user,
                operation_id=operation, status={"draft": "draft", "approve": "approved", "reject": "rejected"}[action],
                **{key: payload[key] for key in CAS_KEYS | extra})
            return JsonResponse({"success": True, "created": created, "request_id": str(request_id),
                "decision_id": str(decision.pk), "commerce": _context(client, request.user, request_id)})
        if action == "resolve_shipping_review":
            from management.services.ig_assisted_partial import resolve_shipping_review
            if set(payload) != {"action", "operation_id", "order_id", "expected_document_ref", "expected_terms_digest"}:
                raise ValueError("shipping review fields invalid")
            if (type(payload["order_id"]) is not int or payload["order_id"] <= 0
                or not isinstance(payload["expected_document_ref"], str) or len(payload["expected_document_ref"]) > 36
                or not commerce._hash(payload["expected_terms_digest"])):
                raise ValueError("shipping review identity invalid")
            result = resolve_shipping_review(client.pk, actor=request.user, operation_id=operation,
                order_id=payload["order_id"], expected_document_ref=payload["expected_document_ref"],
                expected_terms_digest=payload["expected_terms_digest"])
            return JsonResponse({"success": result.get("ready") is True, "shipping_review": result,
                "commerce": _context(client, request.user)}, status=200 if result.get("ready") is True else 409)
        if action in {"issue_partial_offer", "prepare_selected", "issue_continuation_offer"}:
            from management.services import ig_assisted_partial as partial
            extra = ({"decision_id", "expected_decision_digest", "expected_terms_digest"}
                if action != "issue_continuation_offer" else set())
            if action == "prepare_selected":
                extra |= {"task_id", "expected_choice_digest"}
            required = {"action", "operation_id", "request_id", "expected_request_digest", "expected_coverage_digest", *extra}
            if set(payload) != required:
                raise ValueError("partial fields invalid")
            request_id = _uuid(payload["request_id"])
            _request_owned(client, request_id)
            arguments = {key: payload[key] for key in required - {"action", "operation_id", "request_id"}}
            if "decision_id" in arguments:
                arguments["decision_id"] = _uuid(arguments["decision_id"])
            if "task_id" in arguments and (type(arguments["task_id"]) is not int or arguments["task_id"] <= 0):
                raise ValueError("task identity invalid")
            if any(not commerce._hash(value) for key, value in arguments.items() if key.startswith("expected_")):
                raise ValueError("partial digest invalid")
            if action == "prepare_selected":
                result = partial.prepare_selected(client.pk, actor=request.user, request_id=request_id,
                    operation_id=operation, **arguments)
                return JsonResponse({"success": result.get("ready") is True, "checkout": _checkout_payload(result),
                    "commerce": _context(client, request.user, request_id)}, status=200 if result.get("ready") is True else 409)
            task, created = getattr(partial, action)(client.pk, actor=request.user, request_id=request_id,
                operation_id=operation, **arguments)
            return JsonResponse({"success": True, "created": created, "offer": partial._offer_dto(task),
                "commerce": _context(client, request.user, request_id)})
        if action == "stock_sync":
            if set(payload) != {"action", "operation_id", "request_id", "decision_id",
                    "expected_request_digest", "expected_decision_digest", "expected_preview_digest", "counts_by_pool"}:
                raise ValueError("stock fields invalid")
            request_id = _uuid(payload["request_id"])
            _request_owned(client, request_id)
            from management.services.ig_assisted_stock_sync import apply_stock_sync
            result = apply_stock_sync(request_id, actor=request.user, decision_id=_uuid(payload["decision_id"]),
                operation_id=operation, expected_request_digest=payload["expected_request_digest"],
                expected_decision_digest=payload["expected_decision_digest"],
                expected_preview_digest=payload["expected_preview_digest"], counts_by_pool=payload["counts_by_pool"])
            return JsonResponse({"success": result.get("ready") is True, "stock_sync": result,
                "commerce": _context(client, request.user, request_id)},
                status=200 if result.get("ready") is True else 409)
        if action == "prepare":
            if set(payload) != {"action", "operation_id", "request_id", "decision_id",
                    "expected_request_digest", "expected_decision_digest"}:
                raise ValueError("checkout fields invalid")
            request_id = _uuid(payload["request_id"])
            _request_owned(client, request_id)
            from management.services.ig_assisted_checkout import prepare_assisted_checkout
            checkout = prepare_assisted_checkout(client.pk, actor=request.user, request_id=request_id,
                decision_id=_uuid(payload["decision_id"]), operation_id=operation,
                expected_request_digest=payload["expected_request_digest"],
                expected_decision_digest=payload["expected_decision_digest"])
            return JsonResponse({"success": checkout.get("ready") is True, "checkout": _checkout_payload(checkout),
                "commerce": _context(client, request.user, request_id)},
                status=200 if checkout.get("ready") is True else 409)
        raise ValueError("action invalid")
    except commerce.AssistedDecisionError as exc:
        status = 403 if exc.reason == "actor_forbidden" else 410 if exc.reason == "client_erasing" else 409
        return _error(exc.reason, status)
    except (ValueError, TypeError, KeyError, UnicodeDecodeError, OverflowError):
        return _error("assisted_request_invalid", 400)


@login_required(login_url="management_login")
@require_GET
@never_cache
def assisted_commerce_image(request, client_id, image_id):
    try:
        _owner(request, client_id)
        return media.serve_operator_asset(client_id, image_id, actor=request.user)
    except commerce.AssistedDecisionError as exc:
        if exc.reason == "actor_forbidden":
            return _error(exc.reason, 403)
        return HttpResponse("Медіа недоступне.", status=404)
    except (OSError, ValueError):
        return HttpResponse("Медіа недоступне.", status=404)


@login_required(login_url="management_login")
@require_GET
@never_cache
def assisted_order_item_image(request, order_id, item_id):
    from django.shortcuts import get_object_or_404
    from orders.models import Order, OrderItem
    try:
        commerce._actor(getattr(request.user, "pk", None))
        order = get_object_or_404(Order, pk=order_id)
        item = get_object_or_404(OrderItem, pk=item_id, order=order)
        return media.serve_order_asset(order, item, actor=request.user)
    except commerce.AssistedDecisionError as exc:
        if exc.reason == "actor_forbidden":
            return _error(exc.reason, 403)
        return HttpResponse("Медіа недоступне.", status=404)
    except (OSError, ValueError):
        return HttpResponse("Медіа недоступне.", status=404)


urlpatterns = [
    path("bot/api/clients/<int:client_id>/assisted-commerce/", assisted_commerce_api,
        name="management_assisted_commerce_api"),
    path("bot/api/clients/<int:client_id>/assisted-commerce/images/<uuid:image_id>/", assisted_commerce_image,
        name="management_assisted_commerce_image"),
    path("bot/api/orders/<int:order_id>/items/<int:item_id>/assisted-image/", assisted_order_item_image,
        name="management_assisted_order_item_image"),
]
