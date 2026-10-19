"""Read-only discussed-product identity; never selection or checkout authority.

A won image interpretation remains conversation evidence when its reply cannot
be sent. Recompute from immutable source/proposal/winner records instead of
promoting an old financial control or parsing a manager's prose as a purchase.
"""
from datetime import datetime
import hashlib
import re

from django.utils import timezone
from django.db.models import Max


VERSION = "discussed-product-focus.v1"
MAX_CANDIDATES = 12
MAX_LATER_SOURCES = 64
_SWITCH = re.compile(
    r"(?:інш\w*|друг\w*|other|different)\s+(?:принт\w*|дизайн\w*|модел\w*|футболк\w*|print\w*|design\w*|model\w*)"
    r"|\b(?:не|not)\s+(?:цю|эту|this)\s+(?:футболк\w*|модел\w*|принт\w*|shirt|model|print)", re.I)


def _switches_product(text):
    from management.services.ig_reply_language import own_reply_text
    value = own_reply_text(text or "")
    for match in _SWITCH.finditer(value):
        prefix = value[:match.start()]
        if re.search(r"\b(?:не|ні|not|no|don't|do\s+not)(?:\s+(?:хочу|want|need|потрібно|нужно))?\s*$", prefix, re.I):
            continue
        return True
    return False


_GARMENT_SUBJECTS = (
    ("zip_hoodie", "zip_hoodies", r"\b(?:(?:zip|зіп|зип)[ -]?(?:худі|худи|hoodies?)?|худ[іи]\s+(?:на\s+)?(?:блискавц\w*|молни\w*))\b"),
    ("longsleeve", "longsleeves", r"\b(?:лонгслів\w*|лонгслив\w*|long[ -]?sleeves?)\b"),
    ("hoodie", "hoodies", r"\b(?:худі|худи|hoodies?)\b"),
    ("tshirt", "tshirts", r"\b(?:футболк\w*|t[ -]?shirts?|tees?)\b"),
)


def information_category(value):
    """Normalize catalog category aliases without inferring stock or a SKU."""
    value = str(value or "").casefold().replace("-", "_")
    return {"hoodie": "hoodies", "tshirt": "tshirts", "t_shirt": "tshirts", "t_shirts": "tshirts",
        "longsleeve": "longsleeves", "zip_hoodie": "zip_hoodies", "ziphoodie": "zip_hoodies"}.get(value, value)


def information_subject(sources):
    """Current explicit category topic; it never changes retained selections.

    Quoted/negated categories and messages about a garment not wanted are not
    a new subject. Several positive categories remain ambiguous.
    """
    from management.services.ig_reply_language import own_reply_text, _QUOTES
    from management.services.ig_reply_truth import _locally_negated
    latest, subject_texts = {}, []
    for row in list(sources or ())[-64:]:
        if row.get("role", "user") != "user":
            continue
        raw = str(row.get("text") or "")
        text = re.sub(r'"[^"\n]*"|«[^»\n]*»|“[^”\n]*”', " ", own_reply_text(raw))
        flag_text = _QUOTES.sub(" ", re.sub(r"(?m)^\s*>[^\n]*(?:\n|$)", " ", raw)) if len(raw) <= 64000 else ""
        matches, spans = [], []
        for garment, category, pattern in _GARMENT_SUBJECTS:
            for match in re.finditer(pattern, text, re.I):
                if any(start <= match.start() < end for start, end in spans):
                    continue
                spans.append(match.span())
                suffix = text[match.end():match.end() + 35]
                if (_locally_negated(text, match.start()) or re.search(
                    r"\b(?:не|not|don't|do\s+not)\s+(?:хочу|потріб\w*|нуж\w*|want|need)\s*$", text[:match.start()], re.I) or re.match(
                    r"\s*(?:мені\s+|мне\s+)?не\s+(?:треба|потріб\w*|надо|нуж\w*|хочу)", suffix, re.I)):
                    continue
                matches.append((garment, category))
        kinds = list(dict.fromkeys(matches))
        if kinds:
            subject_texts = [flag_text]
            latest = {"schema": "information-subject.v1", "status": "identified" if len(kinds) == 1 else "ambiguous",
                "garment_type": kinds[0][0] if len(kinds) == 1 else "",
                "category_slug": kinds[0][1] if len(kinds) == 1 else "",
                "source_message_ids": [row["message_id"]] if type(row.get("message_id")) is int else [],
                "selection_authority": False, "stock_authority": False, "checkout_authority": False}
        elif latest:
            subject_texts.append(flag_text)
            # A nounless information question in this same owned burst uses
            # the latest explicit category, not an older retained cart item.
            question = re.search(r"\?|\b(?:яки\w*|яка|яке|які|како\w*|какая|какие|чи|скільки|what|which|how)\b", text, re.I)
            historical = re.search(r"\b(?:раніше|раньше|ранее|учора|вчора|вчера|previously|earlier|yesterday|historically)\b|(?:минул\w*|прошл\w*|previous|old)\s+(?:замовлен\w*|заказ\w*|order)", text, re.I)
            topics = re.finditer(r"склад\w*|состав\w*|щільн\w*|плотн\w*|матер[іи]ал\w*|тканин\w*|ткан[ьи]\w*|догляд\w*|прати|стират\w*|посадк\w*|фасон\w*|крій|кро[юїй]|унісекс|унисекс|люверс\w*|манжет\w*|\b(?:composition|density|gsm|fabric|material|care|wash|fit|cut|unisex|eyelets?|cuffs?)\b", text, re.I)
            relevant = any(not _locally_negated(text, match.start()) and not re.search(
                r"\b(?:не|not|no)\s+(?:цікав\w*|интерес\w*|потріб\w*|нуж\w*|want|need)\s*$", text[:match.start()], re.I) and not re.match(
                r"\s*не\s+(?:цікав\w*|интерес\w*|потріб\w*|нуж\w*|треба|надо)", text[match.end():], re.I) for match in topics)
            if question and relevant and not historical and len(text) <= 600 and type(row.get("message_id")) is int:
                latest["source_message_ids"].append(row["message_id"])
    if latest:
        text = "\n".join(subject_texts)
        latest["future_base_requested"] = bool(re.search(
            r"майбутн\w*|будущ\w*|\bfuture\b|(?:наступн\w*|нов\w*|следующ\w*|new|next)\s+(?:парті\w*|парти\w*|основ\w*|колекці\w*|коллекци\w*|batch|bases?|collections?)", text, re.I))
        latest["unsupported_detail_requested"] = bool(re.search(
            r"люверс\w*|манжет\w*|фурнітур\w*|фурнитур\w*|блискав\w*|молни\w*|ґудзик\w*|пуговиц\w*|заклеп\w*|шнур\w*|резинк\w*|гумк\w*|"
            r"віскоз\w*|вискоз\w*|шерст\w*|\bвовн\w*|шовк\w*|шелк\w*|льон\w*|льня\w*|"
            r"\b(?:eyelets?|grommets?|cuffs?|hardware|zippers?|buttons?|drawstrings?|ribbing|viscose|wool|silk|linen|zip)\b|\b100\s*%|(?:без|no|without)\s*(?:фліс\w*|флис\w*|fleece)", text, re.I))
        # Category-level facts cannot establish which photographed, linked or
        # specifically referenced garment/base the customer means.
        photo_text = re.sub(r"(?:фото|photos?|pictures?|images?)\s+(?:кро\w*|фасон\w*|посадк\w*|приклад\w*|пример\w*|fits?|cuts?|examples?)", "", text, flags=re.I)
        latest["specific_product_requested"] = bool(re.search(
            r"\b(?:цей|ця|це|ці|цього|цієї|эту|этот|эта|это|эти|этого|этой|this|that|these|those|same)\b|"
            r"той\s+сам\w*|та\s+сама|те\s+саме|те\s+же|тот\s+же|та\s+же|"
            r"фото\w*|світлин\w*|скрін\w*|скрин\w*|\b(?:photos?|pictures?|images?|screenshots?|sku)\b|"
            r"артикул\w*|https?://\S+/(?:product|products|p)/|\b(?:product|товар)\s*(?:id|№|#|:)?\s*\d+", photo_text, re.I))
    return latest


def _subject_scoped_focus(product, *, sources, basis, source_ids, proof):
    subject = information_subject(sources)
    if subject and (subject["status"] != "identified" or subject.get("future_base_requested") or subject["category_slug"] != information_category(product.category.slug)):
        return {**_empty("different_information_subject"), "information_subject": subject,
            "retained_discussed_product_id": product.pk}
    return _result(product, basis=basis, source_ids=source_ids, proof=proof)


def _empty(reason="focus_not_proven"):
    return {"schema": VERSION, "status": "none", "reason": reason,
        "product_id": None, "title": "", "source_message_ids": [],
        "selection_authority": False, "checkout_authority": False, "stock_authority": False}


def _result(product, *, basis, source_ids, proof):
    from management.services.ig_revision_outbox import _digest
    return {**_empty(""), "status": "focused", "product_id": product.pk, "title": product.title,
        "category_slug": product.category.slug,
        "basis": basis, "source_message_ids": sorted(source_ids), "proof_digest": _digest(proof)}


def _media_identity(owner, *, namespace, recipient, floor, watermark, sealed_at):
    from management.models import InstagramBotMessage
    from management.services.ig_revision_outbox import _digest, revision_sources_current
    from management.services.ig_revision_proposal import _generation_graph_matches

    proposal, snapshots = owner.generation_proposal, owner.bundle_snapshot.get("sources") or []
    if (not proposal or _digest(proposal) != owner.generation_proposal_digest
        or not snapshots or _digest(owner.bundle_snapshot) != owner.snapshot_digest
        or any(row.get("role") != "user" or row.get("source_namespace") != namespace
            or type(row.get("message_id")) is not int or not floor <= row["message_id"] <= watermark
            for row in snapshots)
        or not revision_sources_current(owner)):
        return None
    source_ids = {row["message_id"] for row in snapshots}
    owned = list(InstagramBotMessage.objects.filter(pk__in=source_ids, client_id=owner.client_id,
        sender_id=recipient, provider_namespace=namespace, role="user", source__in=("webhook", "poll"))
        .values("pk", "attachment_media"))
    if len(owned) != len(source_ids):
        return None
    if proposal.get("sources") != [{"message_id": row["message_id"], "source_digest": row["source_digest"],
            "ordinal": row["ordinal"]} for row in snapshots]:
        return None
    intelligence = proposal.get("turn_intelligence") or {}
    product_id = intelligence.get("auto_product_id")
    candidates = intelligence.get("catalog_candidates") or []
    if (type(product_id) is not int or product_id < 1
        or intelligence.get("catalog_resolution") != "auto_select"
        or len(candidates) != 1 or candidates[0].get("product_id") != product_id
        or float(candidates[0].get("confidence") or 0) < 0.9):
        return None
    media, generation = proposal.get("request_media_manifest") or {}, proposal.get("generation") or {}
    items = media.get("items") or []
    if len(items) != 1 or media.get("actual_inline_count") != 1:
        return None
    item = items[0]
    if (item.get("source_message_id") not in source_ids
        or not str(item.get("mime") or "").startswith("image/")
        or media.get("actual_content_hashes") != [item.get("content_hash")]):
        return None
    sealed_parts = [part for source in snapshots if source["message_id"] == item["source_message_id"]
        for part in source.get("media_parts") or [] if part.get("source_part_id") == item.get("source_part_id")]
    if (len(sealed_parts) != 1 or sealed_parts[0].get("capture_outcome") != "owned"
        or sealed_parts[0].get("content_hash") != item.get("content_hash")):
        return None
    current_parts = next(row["attachment_media"] or [] for row in owned if row["pk"] == item["source_message_id"])
    for index, part in enumerate(current_parts):
        if (part.get("source_part_id") == item.get("source_part_id")
            or part.get("original_index", index) == item.get("original_index")):
            if part.get("content_hash") and part["content_hash"] != item.get("content_hash"):
                return None
    request = intelligence.get("media_request") or {}
    observations = intelligence.get("image_observations") or []
    if (request.get("request_id") != generation.get("request_id")
        or request.get("provider_model") != generation.get("actual_model")
        or request.get("actual_inline_count") != 1 or request.get("inline_count_known") is not True
        or request.get("submitted_parts") != [{key: item.get(key) for key in
            ("source_message_id", "source_part_id", "original_index", "content_hash")}]
        or len(observations) != 1 or observations[0].get("outcome") != "understood"
        or observations[0].get("source_part_id") != item.get("source_part_id")
        or observations[0].get("content_hash") != item.get("content_hash")):
        return None
    generated_at = datetime.fromisoformat(generation.get("generated_at") or "")
    if timezone.is_naive(generated_at) or (sealed_at and generated_at > sealed_at):
        return None
    if not _generation_graph_matches(revision=owner, source_ids=source_ids,
            request_id=generation.get("request_id"), model=generation.get("actual_model"),
            policy_manifest=proposal.get("policy_manifest") or {}, lock=False):
        return None
    return {"product_id": product_id, "source_ids": source_ids,
        "proof": {"revision_id": owner.pk, "proposal_digest": owner.generation_proposal_digest,
            "source_digest": owner.snapshot_digest, "request_id": generation["request_id"],
            "media_content_hash": item["content_hash"]}}


def capture_product_focus(revision, *, source_cart_capture=None):
    """Bound the topic to owned sources, reset and current customer corrections."""
    from management.models import IgClient, IgCustomerTurnRevision, InstagramBotMessage, IgCommerceTurnDecision
    from management.services.ig_revision_outbox import _digest, revision_sources_current
    from management.services.ig_funnel_reset import current_message_floor
    from management.services.ig_commerce_turns import parse_turn
    from storefront.models import Product

    try:
        sources = (revision.bundle_snapshot or {}).get("sources") or []
        if (not sources or _digest(revision.bundle_snapshot) != revision.snapshot_digest
            or not revision_sources_current(revision)):
            return _empty("focus_source_unsealed")
        namespace = sources[0].get("source_namespace")
        if not namespace or any(row.get("source_namespace") != namespace for row in sources):
            return _empty("focus_namespace_unproven")
        client = IgClient.objects.filter(pk=revision.client_id, hidden_at__isnull=True,
            privacy_erasure_started_at__isnull=True).first()
        if client is None:
            return _empty("focus_owner_unavailable")
        floor, watermark = current_message_floor(client), max(row["message_id"] for row in sources)
        for source in sources:
            parsed = parse_turn(source.get("text") or "")
            if (parsed.reset_requested or parsed.new_purchase_requested or parsed.exchange_requested
                or parsed.rejected_product_ids or _switches_product(source.get("text") or "")):
                return _empty("customer_changed_product_focus")
        cart = source_cart_capture or {}
        active = next((line for line in cart.get("lines") or [] if line.get("line_id") == cart.get("active_line_id")), {})
        field = (active.get("fields") or {}).get("product_id") or {}
        selection_source = (field.get("source") or {}).get("source_message_id")
        current_media = any(row.get("media_parts") for row in sources)
        if (cart.get("schema") == "source-selections.v1" and cart.get("status") == "captured"
            and (cart.get("scope") or {}).get("client_id") == client.pk
            and (cart.get("scope") or {}).get("episode_id") == client.current_commercial_episode_id
            and field.get("status") == "confirmed" and type(selection_source) is int
            and type(field.get("value")) is int
            and floor <= selection_source <= watermark and not current_media):
            # A retained purchase selection must not override a newer photo
            # inquiry whose identification has not become a checkout choice.
            newer_media = InstagramBotMessage.objects.filter(client_id=client.pk, sender_id=client.igsid,
                provider_namespace=namespace, role="user", source__in=("webhook", "poll"),
                pk__gt=selection_source, pk__lte=watermark).exclude(attachment_media__isnull=True).exclude(
                    attachment_media=[]).exists()
            product = None if newer_media else Product.objects.select_related("category").filter(
                pk=field.get("value"), status="published").first()
            if product is not None:
                return _subject_scoped_focus(product, sources=sources, basis="source_selected_product",
                    source_ids=[selection_source], proof=field)
        owners = list(IgCustomerTurnRevision.objects.filter(client_id=client.pk,
            generation_proposal__turn_intelligence__catalog_resolution="auto_select",
            sources__message_id__gte=floor, sources__message_id__lte=watermark, sources__source_namespace=namespace,
        ).annotate(latest_focus_source=Max("sources__message_id")).order_by("-latest_focus_source", "-pk")[:MAX_CANDIDATES])
        identities = []
        for owner in owners:
            identity = _media_identity(owner, namespace=namespace, recipient=client.igsid, floor=floor,
                watermark=watermark, sealed_at=revision.sealed_at)
            if identity:
                identities.append(identity)
        if not identities:
            return _empty()
        identities.sort(key=lambda value: max(value["source_ids"]), reverse=True)
        identity = identities[0]
        later = list(InstagramBotMessage.objects.filter(client_id=client.pk, sender_id=client.igsid,
            provider_namespace=namespace, role="user", source__in=("webhook", "poll"),
            pk__gt=max(identity["source_ids"]), pk__lte=watermark).exclude(status="failed")
            .order_by("pk")[:MAX_LATER_SOURCES + 1])
        if len(later) > MAX_LATER_SOURCES:
            return _empty("focus_source_scan_capped")
        decisions = {row.source_message_id: row for row in IgCommerceTurnDecision.objects.filter(
            source_message_id__in=[row.pk for row in later], session__client_id=client.pk)}
        for row in later:
            parsed = parse_turn(row.text)
            request = (decisions[row.pk].request_payload or {}) if row.pk in decisions else {}
            changed_product = request.get("exact_product_id") or parsed.exact_product_id
            if (parsed.reset_requested or parsed.new_purchase_requested or parsed.exchange_requested
                or parsed.rejected_product_ids or request.get("rejected_product_ids")
                or request.get("reset_requested") or request.get("new_purchase_requested")
                or _switches_product(row.text or "")
                or (changed_product and changed_product != identity["product_id"])):
                return _empty("customer_changed_product_focus")
            if row.attachment_media:
                return _empty("new_customer_media_requires_identification")
        product = Product.objects.select_related("category").filter(pk=identity["product_id"], status="published").first()
        if product is None:
            return _empty("focus_catalog_product_unavailable")
        return _subject_scoped_focus(product, sources=sources, basis="won_source_media_identification",
            source_ids=identity["source_ids"], proof=identity["proof"])
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError, RecursionError):
        return _empty("focus_proof_invalid")


def product_focus_guidance(focus):
    if not isinstance(focus, dict) or focus.get("status") != "focused":
        return ""
    import json
    return ("[SOURCE-BOUND DISCUSSED PRODUCT]\n" + json.dumps(focus, ensure_ascii=False, separators=(",", ":"))
        + "\nKeep this print/design while answering follow-up fit, size, audience or example-photo questions. "
        "Do not ask which design again or replace it with an unrelated assortment. "
        "A fit example with another print must be explicitly labelled as an example of the cut only. "
        "The customer may explicitly change the design. This is discussion context only: "
        "it grants no selected SKU, stock, price, checkout or payment authority.")


def _retained_fit_preference(revision, focus):
    """Walk only a bounded, evidenced continuation of the same consultation."""
    from management.models import InstagramBotMessage
    from management.services.ig_commerce_turns import parse_turn
    from management.services.ig_fit_consultation import contextual_fit_choice, fit_consultation_request, _CHART
    from management.services.ig_funnel_reset import current_message_floor
    from management.services.ig_reply_language import own_reply_text
    from management.services.ig_reply_truth import _locally_negated
    from management.services.ig_revision_outbox import _digest

    sources = (revision.bundle_snapshot or {}).get("sources") or []
    client = revision.client
    rows = InstagramBotMessage.objects.filter(client_id=client.pk, sender_id=client.igsid,
        provider_namespace=sources[0].get("source_namespace"), role="user", source__in=("webhook", "poll"),
        pk__gte=current_message_floor(client), pk__gt=max(focus.get("source_message_ids") or [0]),
        pk__lt=min(row["message_id"] for row in sources)).exclude(status="failed").order_by("-pk")[:8]
    skipped = []
    for row in rows:
        if row.attachment_media or row.attachments or row.quick_reply_payload:
            return {}
        proof = contextual_fit_choice(client, row)
        if proof:
            if skipped:
                proof = {**proof, "intervening_information_sources": skipped}
                proof["retention_digest"] = _digest(proof)
            return proof
        text = own_reply_text(row.text or "")
        parsed = parse_turn(text)
        if (parsed.reset_requested or parsed.new_purchase_requested or parsed.exchange_requested
            or parsed.purchase_requested or parsed.rejected_product_ids or _switches_product(text)
            or re.search(r"\b(?:худі|худи|hoodies?|світшот\w*|свитшот\w*|sweatshirts?)\b", text, re.I)
            or re.search(r"сказа\w*|казав|казала|говори\w*|просив|просила|просил|asked|said|told", text, re.I)):
            return {}
        request = fit_consultation_request([{"message_id": row.pk, "role": "user", "text": text}])
        chart = _CHART.search(text)
        chart_request = bool(chart and not _locally_negated(text, chart.start()) and re.search(
            r"\?|покаж\w*|надішл\w*|пришл\w*|скин\w*|дайте|send|show|please|можна|можно|can|could|потріб\w*|нуж\w*", text, re.I))
        if (not ((request and not request.get("assortment_requested")) or chart_request)
            or set(parsed.field_updates).intersection({"size", "color", "qty"})):
            return {}
        skipped.append({"source_message_id": row.pk,
            "source_digest": hashlib.sha256(str(row.text or "").encode()).hexdigest()})
    return {}


def capture_discussed_configuration(revision, *, source_cart_capture=None):
    """Prove which discussed cut a chart illustrates without choosing a SKU.

    A processed contextual answer can remain valid customer evidence even when
    its old reducer did not select the product. Only that answer's own delivered
    question proves the cut; a later manager acknowledgement cannot supply it.
    """
    from management.services.ig_funnel_reset import current_message_floor
    from management.services.ig_revision_outbox import _digest

    if source_cart_capture is None:
        from management.services.ig_commerce_projection import capture_current_selection_lines
        source_cart_capture = capture_current_selection_lines(revision.client_id)
    cart = source_cart_capture or {}
    lines = cart.get("lines") or []
    client = revision.client
    sources = (revision.bundle_snapshot or {}).get("sources") or []
    if (cart.get("schema") != "source-selections.v1" or cart.get("status") != "captured"
        or (cart.get("scope") or {}).get("client_id") != revision.client_id
        or (cart.get("scope") or {}).get("episode_id") != client.current_commercial_episode_id
        or len(lines) != 1 or cart.get("active_line_id") != lines[0].get("line_id") or not sources):
        return {}
    watermark, floor = max(row["message_id"] for row in sources), current_message_floor(client)
    if int((cart.get("source_watermark") or {}).get("message_id") or 0) > watermark:
        return {}
    focus = capture_product_focus(revision, source_cart_capture=cart)
    if focus.get("status") != "focused":
        return {}
    fields = lines[0].get("fields") or {}
    field, product = fields.get("fit_option_code") or {}, fields.get("product_id") or {}
    fit, proof = None, {}
    if (field.get("status") == "confirmed" and field.get("authority") == "customer_source"
        and field.get("value") in {"classic", "oversize"}
        and type((field.get("source") or {}).get("source_message_id")) is int
        and floor <= field["source"]["source_message_id"] <= watermark
        and product.get("status") == "confirmed" and product.get("value") == focus["product_id"]):
        fit, proof = field["value"], {"schema": "captured-customer-fit.v1", **field}
    else:
        proof = _retained_fit_preference(revision, focus)
        fit = proof.get("fit")
    if fit not in {"classic", "oversize"}:
        return {}
    result = {"schema": "discussed-product-configuration.v1", "product_id": focus["product_id"],
        "fit_code": fit, "product_focus": focus, "fit_source": proof,
        "scope": {"client_id": client.pk, "episode_id": client.current_commercial_episode_id,
            "revision_id": revision.pk, "snapshot_digest": revision.snapshot_digest,
            "permission_epoch": revision.permission_epoch, "source_namespace": sources[0].get("source_namespace"),
            "reset_floor": current_message_floor(client)},
        "selection_authority": False, "stock_authority": False, "checkout_authority": False}
    result["proof_digest"] = _digest(result)
    return result


def capture_fit_chart_comparison(revision, fit_code):
    """Prove a requested chart alternative without customer selection rights."""
    from management.services.ig_fit_consultation import requested_chart_fits
    from management.services.ig_funnel_reset import current_message_floor
    from management.services.ig_revision_outbox import _digest

    cart = (revision.generation_proposal or {}).get("source_cart_capture")
    if cart is None:
        from management.services.ig_commerce_projection import capture_current_selection_lines
        cart = capture_current_selection_lines(revision.client_id)
    focus = capture_product_focus(revision, source_cart_capture=cart)
    if focus.get("status") != "focused":
        return {}
    from management.services.ig_revision_catalog_assets import capture_fit_photo_capability
    supported = capture_fit_photo_capability(focus["product_id"], metadata_only=True).get("supported_fits", [])
    sources = (revision.bundle_snapshot or {}).get("sources") or []
    requested = requested_chart_fits(sources, supported_fits=supported)
    if not requested or fit_code not in requested or fit_code not in supported:
        return {}
    result = {"schema": "discussed-fit-comparison.v1", "product_id": focus["product_id"], "fit_code": fit_code,
        "product_focus": focus, "fit_source": {"schema": "fit-chart-comparison-source.v1", "requested_fits": requested,
            "source_message_ids": [row["message_id"] for row in sources]},
        "scope": {"client_id": revision.client_id, "episode_id": revision.client.current_commercial_episode_id,
            "revision_id": revision.pk, "snapshot_digest": revision.snapshot_digest, "permission_epoch": revision.permission_epoch,
            "source_namespace": sources[0].get("source_namespace"), "reset_floor": current_message_floor(revision.client)},
        "selection_authority": False, "stock_authority": False, "checkout_authority": False}
    result["proof_digest"] = _digest(result)
    return result


def focus_selection_candidate(revision, focus, *, source_cart_capture=None):
    """A fresh customer selector may nominate the discussed item for validation.

    This does not apply a choice. The current winner must emit product/config
    controls and the existing catalog/action owner must validate and persist
    them before checkout can use the selection.
    """
    from management.services.ig_commerce_turns import parse_turn
    from management.services.ig_reply_language import own_reply_text
    from management.services.ig_revision_outbox import _digest
    from management.services.ig_fit_consultation import fit_consultation_request

    cart = source_cart_capture or {}
    lines = cart.get("lines") or []
    sources = (revision.bundle_snapshot or {}).get("sources") or []
    if (not isinstance(focus, dict) or focus.get("schema") != VERSION or focus.get("status") != "focused"
        or type(focus.get("product_id")) is not int or not focus.get("proof_digest")
        or cart.get("schema") != "source-selections.v1" or cart.get("status") != "captured"
        or (cart.get("scope") or {}).get("client_id") != revision.client_id
        or (cart.get("scope") or {}).get("episode_id") != revision.client.current_commercial_episode_id
        or len(lines) != 1 or cart.get("active_line_id") != lines[0].get("line_id")
        or not sources or _digest(revision.bundle_snapshot) != revision.snapshot_digest
        or revision.bundle_snapshot.get("permission_epoch") != revision.permission_epoch):
        return {}
    source = sources[-1]
    text = own_reply_text(source.get("text") or "")
    if (source.get("role") != "user" or source.get("media_parts")
        or fit_consultation_request([source]) or _switches_product(text)
        or re.search(r"[?]|\b(?:які|який|яка|чи|есть|какие|какой|какая|what|which|does|have|available)\b", text, re.I)
        or re.search(r"\b(?:цю|цей|це|эту|этот|this|that)\b", text, re.I)
        or re.search(r"\b(?:худі|худи|hoodies?|світшот\w*|свитшот\w*|sweatshirts?)\b", text, re.I)):
        return {}
    parsed = parse_turn(text)
    updates = dict(parsed.field_updates)
    contextual_fit = {}
    fields = lines[0].get("fields") or {}
    # The captured canonical requirement may supersede this sealed source.
    # Re-parsing its old size must never undo an audited manager set/clear.
    # A later customer choice will carry customer_source authority instead.
    if any(isinstance(proof, dict) and proof.get("authority") == "audited_correction"
        for proof in (*fields.values(), *(lines[0].get("cleared") or {}).values())):
        return {}
    # Contextual aliases (for example "Звична" after the exact delivered fit
    # question) must already have passed the canonical source reducer. Read its
    # current-source proof; never default an unknown short answer to classic.
    fit_field = fields.get("fit_option_code") or {}
    fit_source = fit_field.get("source") or {}
    if ("fit" not in updates and fit_field.get("status") == "confirmed"
        and fit_field.get("authority") == "customer_source"
        and fit_field.get("value") in {"classic", "oversize"}
        and fit_source.get("source_message_id") == source["message_id"]
        and fit_source.get("source_digest") == hashlib.sha256((source.get("text") or "").encode()).hexdigest()):
        updates["fit"] = fit_field["value"]
    if "fit" not in updates:
        from management.models import InstagramBotMessage
        from management.services.ig_fit_consultation import contextual_fit_choice

        current = InstagramBotMessage.objects.filter(pk=source["message_id"], client_id=revision.client_id).first()
        if current is not None:
            contextual_fit = contextual_fit_choice(revision.client, current)
            if contextual_fit:
                updates["fit"] = contextual_fit["fit"]
    if (parsed.reset_requested or parsed.new_purchase_requested or parsed.exchange_requested
        or parsed.rejected_product_ids or not (parsed.purchase_requested or set(updates).intersection({"fit", "size"}))):
        return {}
    choices = {key: field.get("value") for key, field in fields.items()
        if isinstance(field, dict) and field.get("status") == "confirmed"}
    fit = updates.get("fit") or choices.get("fit_option_code")
    size = updates.get("size") or choices.get("size")
    retained_fit = {}
    if not fit and (updates.get("size") or parsed.purchase_requested):
        # A previously processed short answer may predate the contextual-fit
        # reducer. Revalidate that customer's answer against its own delivered
        # selector; a later human acknowledgement does not create the choice.
        retained_fit = _retained_fit_preference(revision, focus)
        fit = retained_fit.get("fit")
    candidate = {"schema": "focused-selection-candidate.v1", "product_id": focus["product_id"],
        "source_message_id": source["message_id"], "source_digest": source["source_digest"],
        "permission_epoch": revision.permission_epoch, "snapshot_digest": revision.snapshot_digest,
        "focus_proof_digest": focus["proof_digest"], "line_id": lines[0].get("line_id"),
        "basis": "current_customer_purchase_assent" if parsed.purchase_requested else "current_customer_selector",
        "candidate_controls": {"product": focus["product_id"], **({"fit": fit} if fit else {}), **({"size": size} if size else {})},
        "next_selector_hint": "fit" if not fit else "size" if not size else "",
        "selection_applied": False, "stock_authority": False, "checkout_authority": False}
    if retained_fit:
        candidate["retained_fit_source"] = retained_fit
    if contextual_fit:
        candidate["contextual_fit_source"] = contextual_fit
    candidate["digest"] = _digest(candidate)
    return candidate
