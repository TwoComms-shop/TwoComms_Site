"""Bounded manager-owned raster assets in the existing hardened private root."""
from datetime import datetime
import hashlib
from io import BytesIO
import uuid
import warnings

from django.core.files.base import ContentFile
from django.db import transaction
from django.http import Http404, HttpResponse
from django.utils import timezone

from management.services import ig_assisted_commerce as commerce
from management.services.ig_private_media import (
    current_private_media_retention_policy, private_media_storage,
)

MAX_BYTES = 6 * 1024 * 1024
MAX_PIXELS = 24_000_000
MAX_DIMENSION = 12000
FORMATS = ("JPEG", "PNG", "WEBP")
EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


def _fail(reason):
    raise commerce.AssistedDecisionError(reason)


def normalize_raster(upload):
    """Decode only bounded single-frame raster data; re-encode pixels, no EXIF."""
    from PIL import Image, ImageOps, UnidentifiedImageError
    if getattr(upload, "size", 0) > MAX_BYTES:
        _fail("image_size_bound")
    body = upload.read(MAX_BYTES + 1)
    if not isinstance(body, bytes) or not body or len(body) > MAX_BYTES:
        _fail("image_size_bound")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(body), formats=FORMATS) as probe:
                width, height = probe.size
                if (min(width, height) < 1 or max(width, height) > MAX_DIMENSION
                    or width * height > MAX_PIXELS):
                    _fail("image_pixel_bound")
                if getattr(probe, "n_frames", 1) != 1:
                    _fail("image_animation_unsupported")
                probe.verify()
            with Image.open(BytesIO(body), formats=FORMATS) as decoded:
                decoded.load()
                alpha = "A" in decoded.getbands() or "transparency" in decoded.info
                mode = "RGBA" if alpha else "RGB"
                pixels = ImageOps.exif_transpose(decoded).convert(mode)
                width, height = pixels.size
                # A new image has no caller metadata, filename, ICC, EXIF or text.
                clean = Image.new(mode, pixels.size)
                clean.paste(pixels)
                encoded = BytesIO()
                clean.save(encoded, format="PNG" if alpha else "JPEG",
                    **({"compress_level": 6} if alpha else {"quality": 90}))
                result = encoded.getvalue()
    except commerce.AssistedDecisionError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError,
            Image.DecompressionBombError, Image.DecompressionBombWarning):
        _fail("image_decode_invalid")
    if not result or len(result) > MAX_BYTES:
        _fail("image_size_bound")
    return {"body": result, "width": width, "height": height,
        "mime_type": "image/png" if alpha else "image/jpeg",
        "content_digest": hashlib.sha256(result).hexdigest(),
        "upload_digest": hashlib.sha256(body).hexdigest()}


def storage_key(request_id, image_id, mime_type):
    if mime_type not in EXTENSIONS:
        _fail("image_mime_invalid")
    return f"assisted/{commerce._uuid(request_id).hex}/{commerce._uuid(image_id).hex}{EXTENSIONS[mime_type]}"


def _image_current(image, *, client_id=None, now=None):
    now = now or timezone.now()
    if (not image or not commerce._verified(image, "image")
        or client_id is not None and image.client_id != client_id
        or image.state != "active" or image.erased_at is not None
        or image.storage_key != storage_key(image.request_id, image.pk, image.mime_type)
        or not commerce._verified(image.request, "request")
        or image.request.client_id != image.client_id
        or image.client.privacy_erasure_started_at is not None or image.client.hidden_at is not None
        or image.client.reply_permission_epoch != image.request.client_permission_epoch):
        _fail("image_unavailable")
    if image.delete_after <= now and not has_business_retention(image):
        _fail("image_unavailable")
    return image


def upload_assisted_image(request_id, *, actor, operation_id, expected_request_version,
        expected_request_digest, expected_full_capture_digest, upload,
        expected_decision_version=0, expected_draft_digest="", now=None):
    """Idempotent upload under the same current source/request/draft CAS."""
    from management.ig_assisted_commerce_models import (
        IgAssistedCommerceRequest as Request, IgAssistedCommerceDecision as Decision,
        IgAssistedCommerceImage as Image,
    )
    from management.models import IgClient
    from management.services.ig_commercial_episodes import commercial_episode_client_lock
    request_id, operation = commerce._uuid(request_id), commerce._uuid(operation_id)
    actor = commerce._actor(getattr(actor, "pk", None))
    identity = Request.objects.filter(pk=request_id).values("client_id").first()
    if not identity:
        _fail("request_missing")
    prepared = normalize_raster(upload)
    now = now or timezone.now()
    input_digest = commerce._digest({"request_id": str(request_id), "actor_id": actor.pk,
        "expected_request_version": expected_request_version, "expected_request_digest": expected_request_digest,
        "expected_full_capture_digest": expected_full_capture_digest,
        "expected_decision_version": expected_decision_version, "expected_draft_digest": expected_draft_digest,
        "upload_digest": prepared["upload_digest"]})
    saved = None
    storage = None
    try:
        with commercial_episode_client_lock(identity["client_id"]), transaction.atomic():
            actor = commerce._actor(actor.pk)
            client = IgClient.objects.select_for_update().get(pk=identity["client_id"])
            request = Request.objects.select_for_update().get(pk=request_id, client=client)
            commerce._current_request(request, client, now)
            if (request.version != expected_request_version or request.envelope_hmac != expected_request_digest
                or request.full_capture_digest != expected_full_capture_digest):
                _fail("request_expectation_changed")
            prior = Image.objects.select_related("request", "client").filter(operation_id=operation).first()
            if prior:
                if (prior.request_id != request_id or prior.uploader_id != actor.pk
                    or prior.envelope.get("operation_input_digest") != input_digest):
                    _fail("operation_conflict")
                _image_current(prior, client_id=client.pk, now=now)
                return prior, False
            head = Decision.objects.filter(request=request).order_by("-version").first()
            if (head and not commerce._verified(head, "decision")
                or (head.version if head else 0) != expected_decision_version
                or (head.draft_digest if head else "") != expected_draft_digest):
                _fail("decision_head_changed")
            image_id = uuid.uuid4()
            key = storage_key(request.pk, image_id, prepared["mime_type"])
            retention = current_private_media_retention_policy()
            deadline = datetime.fromisoformat(retention["delete_after"])
            storage = private_media_storage()
            saved = storage.save(key, ContentFile(prepared["body"]))
            if saved != key:
                _fail("image_storage_conflict")
            image = commerce._seal(Image(id=image_id, request=request, client=client, uploader=actor,
                operation_id=operation, storage_key=key, content_digest=prepared["content_digest"],
                mime_type=prepared["mime_type"], byte_size=len(prepared["body"]),
                width=prepared["width"], height=prepared["height"], delete_after=deadline), "image",
                {"schema": "ig-assisted-owned-raster.v1", "request_id": str(request.pk),
                    "request_digest": request.envelope_hmac, "request_version": request.version,
                    "client_id": client.pk, "uploader_id": actor.pk,
                    "upload_digest": prepared["upload_digest"], "retention": retention},
                operation_input_digest=input_digest)
            return image, True
    except Exception:
        if saved and storage:
            storage.delete(saved)
        raise


def image_payload(image):
    from django.urls import reverse
    return {"id": str(image.pk), "content_digest": image.content_digest,
        "mime_type": image.mime_type, "width": image.width, "height": image.height,
        "preview_url": reverse("management_assisted_commerce_image", args=[image.client_id, image.pk])}


def _bytes_response(image):
    _image_current(image)
    with private_media_storage().open(image.storage_key, "rb") as stream:
        body = stream.read(MAX_BYTES + 1)
    if len(body) != image.byte_size or hashlib.sha256(body).hexdigest() != image.content_digest:
        _fail("image_bytes_changed")
    response = HttpResponse(body, content_type=image.mime_type)
    response["Cache-Control"] = "private, no-store, no-cache, must-revalidate, max-age=0"
    response["X-Content-Type-Options"] = "nosniff"
    response["X-Robots-Tag"] = "noindex, nofollow"
    response["Cross-Origin-Resource-Policy"] = "same-origin"
    response["Content-Disposition"] = 'inline; filename="item-image' + EXTENSIONS[image.mime_type] + '"'
    response["Vary"] = "Cookie"
    return response


def _approved_photo(execution, photo_row):
    from management.ig_assisted_commerce_models import IgAssistedCommerceImage as Image
    if (not commerce._verified(execution, "execution")
        or not commerce._verified(execution.request, "request")
        or not commerce._verified(execution.decision, "decision")
        or execution.decision.request_id != execution.request_id
        or execution.client_id != execution.request.client_id
        or execution.request_digest != execution.request.envelope_hmac
        or execution.decision_digest != execution.decision.envelope_hmac
        or execution.decision.status != "approved"):
        _fail("image_execution_unverified")
    image_id = commerce._uuid(photo_row.get("photo_asset_id"), "image_identity_invalid")
    digest = photo_row.get("photo_digest")
    if not commerce._hash(digest):
        _fail("image_digest_invalid")
    line_id = photo_row.get("line_id") or (photo_row.get("assisted_line") or {}).get("line_id")
    lines = execution.decision.envelope["snapshot"]["items"]
    matching = [line for line in lines if line.get("line_id") == line_id
        and str(line.get("image_id")) == str(image_id) and line.get("image_digest") == digest]
    if len(matching) != 1:
        _fail("image_approval_binding_changed")
    from management.services.ig_checkout_amendment_successors import image_request_owners
    image = Image.objects.select_related("request", "client").filter(pk=image_id,
        request_id__in=image_request_owners(execution.request), client_id=execution.client_id, content_digest=digest).first()
    if not image:
        _fail("image_owner_changed")
    return image


def serve_proposal_asset(proposal, item, validated_grant):
    """The storefront parses its native session grant; this binds the actual item."""
    from management.ig_assisted_commerce_models import IgAssistedCommerceExecution as Execution
    from management.models import IgCheckoutAccessToken
    from management.services.ig_assisted_checkout import validate_assisted_proposal
    from management.services.ig_commercial_episodes import commercial_episode_client_lock
    now = timezone.now()
    if (not isinstance(validated_grant, dict) or validated_grant.get("proposal_id") != str(proposal.public_id)
        or type(validated_grant.get("expires_at")) is not int or validated_grant["expires_at"] <= int(now.timestamp())
        or validated_grant.get("revision") != proposal.revision
        or not validated_grant.get("grant_id") or item.proposal_id != proposal.pk
        or not IgCheckoutAccessToken.objects.filter(pk=validated_grant.get("token_id"), proposal=proposal,
            revoked_at__isnull=True, expires_at__gt=now).exists()):
        raise Http404
    with commercial_episode_client_lock(proposal.client_id), transaction.atomic():
        validation = validate_assisted_proposal(proposal, now=now, fresh=False)
        if not isinstance(validation, dict) or validation.get("ready") is not True:
            raise Http404
        rows = [row for row in validation.get("custom_rows", []) if row.get("proposal_item_id") == item.pk]
        if len(rows) != 1 or not rows[0].get("photo_asset_id"):
            raise Http404
        execution = Execution.objects.select_related("request", "decision").filter(proposal=proposal).first()
        return _bytes_response(_approved_photo(execution, rows[0]))


def _order_photo_proof(order, *, now=None):
    """The checkout owner authenticates historical payment and canonical mapping."""
    from management.ig_assisted_commerce_models import IgAssistedCommerceExecution as Execution
    from management.services.ig_assisted_checkout import validate_assisted_order
    proof = validate_assisted_order(order, now=now or timezone.now())
    if not isinstance(proof, dict) or proof.get("ready") is not True:
        _fail("image_order_proof_unavailable")
    rows = proof.get("item_map")
    if (not isinstance(rows, list) or not rows or len(rows) > commerce.MAX_LINES
        or any(not isinstance(row, dict) or not commerce._positive(row.get("order_item_id"))
            or not commerce._positive(row.get("proposal_item_id")) for row in rows)
        or len({row["order_item_id"] for row in rows}) != len(rows)
        or {row["order_item_id"] for row in rows} != set(order.items.values_list("pk", flat=True))):
        _fail("image_order_item_map_changed")
    execution = Execution.objects.select_related("request", "decision").filter(
        pk=commerce._uuid(proof.get("execution_id")), order=order).first()
    if not execution or not commerce._verified(execution, "execution"):
        _fail("image_order_execution_unverified")
    return execution, rows


def has_business_retention(image):
    """Only actual canonical paid Order/photo proof extends unconverted expiry."""
    from management.ig_assisted_commerce_models import IgAssistedCommerceExecution as Execution
    if not commerce._verified(image, "image"):
        return False
    candidates = list(Execution.objects.select_related("order").filter(
        request_id=image.request_id, client_id=image.client_id, order__isnull=False)[:2])
    if len(candidates) != 1:
        return False
    try:
        execution, rows = _order_photo_proof(candidates[0].order)
        matching = [row for row in rows if str(row.get("photo_asset_id")) == str(image.pk)
            and row.get("photo_digest") == image.content_digest]
        return bool(matching and all(_approved_photo(execution, row).pk == image.pk for row in matching))
    except (commerce.AssistedDecisionError, ValueError, KeyError, TypeError):
        return False


def serve_order_asset(order, item, *, actor=None, account_user=None):
    """Staff need current capabilities; account access needs the exact Order.user."""
    from django.contrib.auth import get_user_model
    from orders.models import Order, OrderItem
    from management.services.ig_commercial_episodes import commercial_episode_client_lock
    if actor is not None:
        commerce._actor(getattr(actor, "pk", None))
    else:
        user = get_user_model().objects.filter(pk=getattr(account_user, "pk", None), is_active=True).first()
        if not user or not getattr(account_user, "is_authenticated", False) or order.user_id != user.pk:
            raise Http404
    if item.order_id != order.pk:
        raise Http404
    execution, rows = _order_photo_proof(order)
    matching = [row for row in rows if row["order_item_id"] == item.pk]
    if len(matching) != 1 or not matching[0].get("photo_asset_id"):
        raise Http404
    with commercial_episode_client_lock(execution.client_id), transaction.atomic():
        actual_order = Order.objects.select_for_update().filter(pk=order.pk).first()
        actual_item = OrderItem.objects.filter(pk=item.pk, order=actual_order).first() if actual_order else None
        if actual_order is None or actual_item is None:
            raise Http404
        if actor is not None:
            commerce._actor(actor.pk)
        else:
            user = get_user_model().objects.select_for_update().filter(
                pk=getattr(account_user, "pk", None), is_active=True).first()
            if user is None or actual_order.user_id != user.pk:
                raise Http404
        execution, rows = _order_photo_proof(actual_order)
        matching = [row for row in rows if row["order_item_id"] == actual_item.pk]
        if len(matching) != 1 or not matching[0].get("photo_asset_id"):
            raise Http404
        return _bytes_response(_approved_photo(execution, matching[0]))


def serve_operator_asset(client_id, image_id, *, actor):
    from management.ig_assisted_commerce_models import IgAssistedCommerceImage as Image, IgAssistedCommerceExecution as Execution
    from management.services.ig_commercial_episodes import commercial_episode_client_lock
    actor = commerce._actor(getattr(actor, "pk", None))
    image = Image.objects.select_related("request", "client").filter(pk=image_id, client_id=client_id).first()
    if not image:
        raise Http404
    with commercial_episode_client_lock(client_id), transaction.atomic():
        commerce._actor(actor.pk)
        executions = list(Execution.objects.select_related("proposal", "request", "decision").filter(
            request_id=image.request_id, client_id=client_id)[:2])
        if not executions:
            commerce._current_request(image.request, image.client, timezone.now())
        elif len(executions) == 1:
            from management.services.ig_assisted_checkout import validate_assisted_proposal
            execution = executions[0]
            validation = validate_assisted_proposal(execution.proposal, fresh=False)
            if not isinstance(validation, dict) or validation.get("ready") is not True:
                _fail("image_execution_unverified")
            rows = [row for row in validation.get("custom_rows", []) if str(row.get("photo_asset_id")) == str(image.pk)
                and row.get("photo_digest") == image.content_digest]
            if not rows or any(_approved_photo(execution, row).pk != image.pk for row in rows):
                _fail("image_approval_binding_changed")
        else:
            _fail("image_execution_unverified")
        return _bytes_response(image)


def delete_asset_bytes(image):
    """Erasure owner supplies verified metadata; never accepts a caller path."""
    if not commerce._verified(image, "image") or image.storage_key != storage_key(image.request_id, image.pk, image.mime_type):
        _fail("image_unverified")
    private_media_storage().delete(image.storage_key)
