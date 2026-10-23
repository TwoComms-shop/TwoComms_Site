"""Source-bound catalogue browsing, independent of cart and physical stock.

Only persisted published merchandise and active collection membership grant
presentation rights. Public links are narrower capabilities, not model URLs.
"""
from __future__ import annotations

from copy import deepcopy
import re
from urllib.parse import urlencode, urlsplit

from django.conf import settings
from django.urls import reverse
from django.utils.translation import override

from management.services.ig_revision_outbox import _digest

SCHEMA = "catalog-browse.v1"
MAX_PRODUCTS = 250
MAX_COLLECTIONS = 250
MAX_SOURCES = 64
_HASH = re.compile(r"[a-f0-9]{64}")
_BROWSE = re.compile(
    r"покаж\w*|показат\w*|скин\w*|надішл\w*|пришл\w*|каталог\w*|асортимент\w*|ассортимент\w*|"
    r"\b(?:show|send|browse|catalog|catalogue)\b|"
    # An existence question must name merchandise. Sizes, colours, prices,
    # payment and delivery remain information requests under their own owners.
    r"\b(?:які|какие|what)\s+(?:(?:є|есть|are(?:\s+there)?)(?:\s+the)?\s+)?"
    r"(?:модел\w*|models?|категор\w*|categor(?:y|ies)|товар\w*|products?|футбол\w*|t[ -]?shirts?|tees?|худі|худи|hoodies?|"
    r"лонгслів\w*|лонгслив\w*|long[ -]?sleeves?|одяг\w*|одежд\w*|clothing|garments?|items?|"
    r"дизайн\w*|принт\w*|тем\w*|designs?|prints?|themes?)\b", re.I)
_ALL = re.compile(r"\b(?:все|всі|усі|увесь|весь|all|everything|каталог\w*|catalog(?:ue)?)\b", re.I)
_NEGATIVE = re.compile(r"\b(?:не|ні|без|крім|окрім|кроме|not|no|without|except|excluding)\b", re.I)
_NOT_ONLY = re.compile(r"\b(?:не\s+(?:тільки|лише|только)|not\s+(?:only|just))\b", re.I)
_ADDITIVE_PAIR = re.compile(r"\b(?:а\s+(?:й|і|також)|но\s+и|but\s+also)\b", re.I)
_CONTRAST_BOUNDARY = r"[;.!?\n]|\b(?:а|але|но|but)\b"
_OR = re.compile(r"\b(?:або|чи|или|or)\b", re.I)
_GARMENTS = {
    "tshirt": re.compile(r"\b(?:футбол\w*|t[ -]?shirts?|tees?)\b", re.I),
    "hoodie": re.compile(r"\b(?:худі|худи|hoodies?)\b", re.I),
    "longsleeve": re.compile(r"\b(?:лонгслів\w*|лонгслив\w*|long[ -]?sleeves?)\b", re.I),
}
_COLLECTION_WORDS = {
    "kharkiv": re.compile(r"\b(?:харків\w*|харков\w*|kharkiv)\b", re.I),
    "military": re.compile(r"\b(?:військов\w*|военн\w*|мілітарі|милитари|military)\b", re.I),
    "streetwear": re.compile(r"\b(?:streetwear|стрітвір\w*|стритвир\w*)\b", re.I),
    "minimalism": re.compile(r"\b(?:minimalism|minimalist\w*|мінімалізм\w*|минимализм\w*)\b", re.I),
}


def _own_text(value):
    from management.services.ig_reply_language import own_reply_text
    return own_reply_text(str(value or ""))


def _generic_photo_request(text):
    # A photo command without a new catalogue predicate retains the existing
    # focused/ordinary media owner. Unknown named themes are not erased here.
    text = re.sub(r"^\s*(?:[2-5]?x{0,3}[sml]|[sml]|one\s*size)\s*[.,;]\s*", "", text, flags=re.I)
    return bool(re.fullmatch(r"\s*(?:покаж\w*|надішл\w*|надiшл\w*|скин\w*|пришл\w*|покажит\w*|(?:please\s+)?(?:show|send)(?:\s+me)?)"
        r"\s+(?:(?:саме|її|його|цього|эту|его|е[её]|the|its|this|a|some)\s+)*(?:фото|фотограф\w*|photos?|pictures?|images?)\s*[.!?]*\s*", text, re.I))


def _negative(text, start):
    # Commas and conjunctions can continue an exclusion list. A contrast starts
    # a new predicate: "не футболки а худі" excludes only the first garment.
    prefix = re.split(_CONTRAST_BOUNDARY, text[:start], flags=re.I)[-1]
    return bool(_NEGATIVE.search(_NOT_ONLY.sub(" ", prefix)[-45:]))


def _additive_mention(text, start):
    # A standalone "not only military" asks for a broader assortment. A paired
    # "not only military but also Kharkiv" retains both explicit alternatives.
    return (not _ADDITIVE_PAIR.search(text)
        and bool(re.search(_NOT_ONLY.pattern + r"\s*$", text[:start], re.I)))


def _unknown_named_predicate(text):
    """Keep an explicit unknown print/theme instead of erasing it into a type."""
    for match in re.finditer(r"\b(?:дизайн\w*|принт\w*|тем\w*|designs?|prints?|themes?|з|із|с|with)\b\s+([^.!?;,\n]+)", text, re.I):
        remainder = match[1]
        for pattern in _GARMENTS.values():
            remainder = pattern.sub(" ", remainder)
        remainder = re.sub(r"\b(?:на|для|з|із|с|со|в|у|та|і|й|и|або|или|on|for|with|and|or|the|a|some|"
            r"фото|photos?|pictures?|images?|дизайн\w*|принт\w*|тем\w*|designs?|prints?|themes?)\b", " ", remainder, flags=re.I)
        if re.search(r"\w", remainder):
            return True
    return False


def _mentions(text, values, *, numeric=False):
    found = []
    for value in sorted({str(v).strip().casefold() for v in values if str(v or "").strip()}, key=lambda v: (-len(v), v)):
        for match in re.finditer(r"(?<!\w)" + re.escape(value) + r"(?!\w)", text, re.I):
            found.append((match.start(), _negative(text, match.start())))
    return found


def _locale(value):
    return value if value in {"uk", "ru", "en"} else "uk"


def _origin():
    base = str(getattr(settings, "SITE_BASE_URL", "") or "https://twocomms.shop").rstrip("/")
    parsed = urlsplit(base)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
        or parsed.path or parsed.query or parsed.fragment or parsed.port not in {None, 443}):
        raise ValueError("catalog_browse_origin_invalid")
    return base


def navigation_url(value):
    """Pure reconstruction of supported routes, never a guessed facet API."""
    if not isinstance(value, dict) or set(value) != {"kind", "category_slug", "collection_slug", "locale", "label", "url"}:
        raise ValueError("catalog_browse_navigation_invalid")
    locale = _locale(value["locale"])
    if locale != value["locale"] or not 0 < len(value["label"]) <= 20:
        raise ValueError("catalog_browse_navigation_invalid")
    slug = value["collection_slug"]
    if not isinstance(slug, str) or not re.fullmatch(r"[-a-zA-Z0-9_]{1,80}", slug):
        raise ValueError("catalog_browse_navigation_invalid")
    with override(locale):
        if value["kind"] == "merch" and not value["category_slug"]:
            path = reverse("merch_collection", kwargs={"collection_slug": slug})
        elif value["kind"] in {"theme", "collection"} and value["category_slug"] in {"tshirts", "hoodie", "long-sleeve"}:
            path = reverse("catalog_by_cat", kwargs={"cat_slug": value["category_slug"]})
            path += "?" + urlencode({value["kind"]: slug})
        else:
            raise ValueError("catalog_browse_navigation_unsupported")
    result = _origin() + path
    if value["url"] and value["url"] != result:
        raise ValueError("catalog_browse_navigation_changed")
    return result


def _seal(payload):
    result = deepcopy(payload)
    result["capture_digest"] = _digest(result)
    return result


def valid_capture(capture):
    return (isinstance(capture, dict) and capture.get("schema") == SCHEMA
        and isinstance(capture.get("capture_digest"), str)
        and _HASH.fullmatch(capture["capture_digest"]) is not None
        and capture["capture_digest"] == _digest({k: v for k, v in capture.items() if k != "capture_digest"}))


def capture_catalog_browse(revision, *, locale=None):
    """Read the sealed current source only; history never invents a browse choice."""
    from django.db.models import Prefetch
    from storefront.models import Product
    from product_catalog.models import MerchCollection, ProductMerchCollection
    from management.services.ig_catalog_graph import _GARMENT_TYPES_BY_CATEGORY
    from storefront.services.product_sales_semantics import GENERIC_COMMERCE_ALIASES

    bundle = getattr(revision, "bundle_snapshot", None) or {}
    sources = bundle.get("sources") or []
    authored = [row for row in sources if isinstance(row, dict) and row.get("role", "user") == "user"
        and not row.get("quick_reply_payload") and _BROWSE.search(_own_text(row.get("text")))
        and not _generic_photo_request(_own_text(row.get("text")))]
    if not authored:
        return {}
    if (not getattr(revision, "pk", None) or not getattr(revision, "client_id", None)
        or not getattr(revision, "snapshot_digest", "") or _digest(bundle) != revision.snapshot_digest
        or not 0 < len(sources) <= MAX_SOURCES
        or any(type(row.get("message_id")) is not int or row["message_id"] <= 0 or not row.get("source_namespace") for row in authored)
        or len({row["message_id"] for row in authored}) != len(authored)
        or len({row["source_namespace"] for row in authored}) != 1):
        return _seal({"schema": SCHEMA, "status": "unavailable", "reason": "catalog_browse_source_unproven",
            "eligible_product_ids": [], "requests": [], "navigation": {}})
    from management.services.ig_reply_language import resolve_own_source_reply_language
    language = _locale(locale or resolve_own_source_reply_language(sources=authored,
        profile_language=getattr(revision.client, "language", "uk")).template_family)
    collections = list(MerchCollection.objects.filter(is_active=True).order_by("pk")[:MAX_COLLECTIONS + 1])
    assignments = ProductMerchCollection.objects.filter(collection__is_active=True).select_related("collection").order_by("collection_id")
    products = list(Product.objects.filter(status="published", category__is_active=True).select_related("category")
        .prefetch_related(Prefetch("merch_collection_assignments", queryset=assignments))
        .order_by("-priority", "-featured", "pk")[:MAX_PRODUCTS + 1])
    scope = {"revision_id": revision.pk, "client_id": revision.client_id,
        "snapshot_digest": revision.snapshot_digest, "source_ids": [row["message_id"] for row in authored],
        "namespace": authored[0]["source_namespace"], "locale": language}
    if len(collections) > MAX_COLLECTIONS or len(products) > MAX_PRODUCTS:
        return _seal({"schema": SCHEMA, "scope": scope, "status": "unavailable", "reason": "catalog_browse_catalog_bound",
            "eligible_product_ids": [], "requests": [], "navigation": {}})
    by_id = {row.pk: row for row in collections}
    def descendants(pk):
        found, pending = {pk}, [pk]
        while pending:
            parent = pending.pop()
            for child in collections:
                if child.parent_id == parent and child.pk not in found:
                    found.add(child.pk)
                    pending.append(child.pk)
        return found
    descendant_ids = {row.pk: descendants(row.pk) for row in collections}
    facts = [{"product_id": row.pk, "title": row.title, "category_slug": row.category.slug,
        "garment_type": _GARMENT_TYPES_BY_CATEGORY.get(row.category.slug, ""),
        "collection_ids": sorted(a.collection_id for a in row.merch_collection_assignments.all())} for row in products]
    taxonomy = [{"id": row.pk, "slug": row.slug, "parent_id": row.parent_id, "kind": row.kind,
        "indexable": row.indexable, "names": [row.name_uk, row.name_ru, row.name_en]} for row in collections]
    collection_labels = {str(value).strip().casefold() for row in collections
        for value in (row.slug, row.name_uk, row.name_ru, row.name_en) if value}
    numeric_codes = {}
    for row in collections:
        for value in {str(v).strip() for v in (row.slug, row.name_uk, row.name_ru, row.name_en) if v}:
            if value.isdecimal():
                numeric_codes.setdefault(value, set()).add(row.pk)
    requests, eligible = [], set()
    for source in authored:
        text = _own_text(source.get("text"))
        # Separate compact command/code/garment tokens without changing the
        # sealed source or breaking normal size tokens such as 2XL.
        text = re.sub(r"(?<=[^\W\d_])(?=\d)", " ", text)
        text = re.sub(r"(?<=\d)(?=(?:худі|худи|hoodie|футбол|tshirt|tee)\b)", " ", text, flags=re.I)
        include, exclude, garments, excluded_garments, named = set(), set(), set(), set(), set()
        numeric_ambiguous, additive = False, False
        for garment, pattern in _GARMENTS.items():
            for match in pattern.finditer(text):
                if _additive_mention(text, match.start()):
                    additive = True
                    continue
                (excluded_garments if _negative(text, match.start()) else garments).add(garment)
        for row in collections:
            labels = [value for value in (row.slug, row.name_uk, row.name_ru, row.name_en)
                if value and not str(value).strip().isdecimal()]
            for _offset, negated in _mentions(text, labels):
                if _additive_mention(text, _offset):
                    additive = True
                    continue
                (exclude if negated else include).add(row.pk)
            pattern = _COLLECTION_WORDS.get(row.slug)
            if pattern:
                for match in pattern.finditer(text):
                    if _additive_mention(text, match.start()):
                        additive = True
                        continue
                    (exclude if _negative(text, match.start()) else include).add(row.pk)
        for match in re.finditer(r"(?<!\w)\d{1,6}(?!\w)", text):
            code = match.group(0)
            codes = numeric_codes.get(code, set())
            after = text[match.end():]
            before_garment = any(re.match(r"\s*" + pattern.pattern, after, re.I) for pattern in _GARMENTS.values())
            design_cue = bool(re.search(r"дизайн\w*|принт\w*|бригад\w*|\b(?:design|print|brigade)\b", text, re.I))
            if codes:
                (exclude if _negative(text, match.start()) else include).update(codes)
                if len(codes) != 1 or (before_garment and not design_cue):
                    numeric_ambiguous = True
            elif garments and (design_cue or not before_garment or int(code) >= 100):
                # Unknown apparent codes cannot silently become all garments.
                # Small bare pre-garment counts remain ordinary quantities.
                numeric_ambiguous = True
        # A concrete brigade name also contains the generic parent word.
        # Its ancestor is already implied, never an additional OR alternative.
        include -= {parent for parent in include if any(child != parent and child in descendant_ids[parent] for child in include)}
        for row in facts:
            # Generic titles are not exact design identities.
            if len(row["title"].strip()) > 3 and row["title"].casefold() not in collection_labels | GENERIC_COMMERCE_ALIASES:
                if any(not negated for _offset, negated in _mentions(text, (row["title"],))):
                    named.add(row["product_id"])
        mode = "any" if len(include) > 1 and (_OR.search(text) or _ADDITIVE_PAIR.search(text)) else "all"
        unresolved = numeric_ambiguous or not (include or garments or excluded_garments or exclude or named or additive or _ALL.search(text))
        # An explicitly named unknown design cannot become a generic category shortlist.
        if (not named and not include and (_unknown_named_predicate(text)
            or (not additive and re.search(r"\b(?:дизайн\w*|принт\w*|design|print)\b", text, re.I)
                and re.search(r"\b(?:тільки|лише|саме|только|only|exact)\b", text, re.I)))):
            unresolved = True
        matching = []
        for fact in facts:
            members = set(fact["collection_ids"])
            included = [bool(members & descendant_ids[pk]) for pk in include]
            if (unresolved or (garments and fact["garment_type"] not in garments)
                or fact["garment_type"] in excluded_garments or (named and fact["product_id"] not in named)
                or any(members & descendant_ids[pk] for pk in exclude)
                or (included and not (any(included) if mode == "any" else all(included)))):
                continue
            matching.append(fact["product_id"])
        requests.append({"source_id": source["message_id"], "source_digest": source.get("source_digest") or _digest(source),
            "garments": sorted(garments), "excluded_garments": sorted(excluded_garments),
            "include": sorted(include), "exclude": sorted(exclude), "mode": mode, "named_product_ids": sorted(named),
            "clarification_reason": "numeric_design_or_quantity" if numeric_ambiguous else "",
            "status": "unresolved" if unresolved else "resolved", "eligible_product_ids": matching})
        eligible.update(matching)
    navigation = {}
    # One exact predicate only: public filters have no exclusion or OR operator.
    if len(requests) == 1:
        request = requests[0]
        if (request["status"] == "resolved" and len(request["include"]) == 1
            and not request["exclude"] and not request["excluded_garments"] and not request["named_product_ids"]):
            collection = by_id[request["include"][0]]
            for fact in facts:
                if fact["product_id"] not in eligible:
                    continue
                category = fact["category_slug"]
                # A per-garment themed link is exact for this card's type; its
                # label never claims to include the other requested types.
                if category not in {"tshirts", "hoodie", "long-sleeve"}:
                    continue
                kind = "collection" if collection.parent_id else "theme"
                if kind == "theme" and collection.kind not in {"theme", "city"}:
                    continue
                label = (getattr(collection, "name_" + language) or collection.name_uk).strip()
                if not 0 < len(label) <= 20:
                    label = {"uk": "Потрібна тема", "ru": "Нужная тема", "en": "Requested theme"}[language]
                nav = {"kind": kind, "category_slug": category, "collection_slug": collection.slug,
                    "locale": language, "label": label, "url": ""}
                nav["url"] = navigation_url(nav)
                navigation[str(fact["product_id"])] = nav
    return _seal({"schema": SCHEMA, "scope": scope, "status": "captured", "requests": requests,
        "complete": True,
        "eligible_product_ids": [row["product_id"] for row in facts if row["product_id"] in eligible],
        "product_facts": [row for row in facts if row["product_id"] in eligible],
        "catalog_digest": _digest({"products": facts, "taxonomy": taxonomy}), "navigation": navigation,
        "selection_authority": False, "stock_authority": False, "consent_authority": False})


def browse_reference(capture):
    if not valid_capture(capture) or capture.get("status") != "captured":
        raise ValueError("catalog_browse_capture_unavailable")
    # The content address already commits to schema, source and full catalogue
    # digest. Do not repeat those 64-byte hashes in each 4096-byte binding.
    return {"digest": capture["capture_digest"], "locale": capture["scope"]["locale"]}


def current_browse_capture(reference, client, invitation_id):
    """Reconstruct from the immutable owned revision, then recheck live catalogue."""
    from management.models import IgCustomerTurnRevision
    if (not isinstance(reference, dict) or set(reference) != {"digest", "locale"}
        or reference["locale"] not in {"uk", "ru", "en"}
        or not isinstance(reference["digest"], str) or not _HASH.fullmatch(reference["digest"])):
        raise ValueError("catalog_browse_reference_invalid")
    revision = IgCustomerTurnRevision.objects.select_related("client").filter(pk=invitation_id, client_id=client.pk).first()
    if revision is None:
        raise ValueError("catalog_browse_revision_missing")
    capture = capture_catalog_browse(revision, locale=reference["locale"])
    if browse_reference(capture) != reference:
        raise ValueError("catalog_browse_context_changed")
    return capture


def validate_catalog_browse_response(capture, response):
    """Hard presentation eligibility; never depends on an unrelated price guard."""
    from management.services.ig_catalog_media import parse_product_ids
    if not capture:
        return ""
    if not valid_capture(capture):
        return "catalog_browse_capture_unavailable" if response.control.get("show_products") else ""
    raw = response.control.get("show_products")
    if raw and capture.get("status") != "captured":
        return "catalog_browse_capture_unavailable"
    if not raw:
        unknown = (capture.get("status") == "unavailable" or
            any(row.get("status") == "unresolved" for row in capture.get("requests", ())))
        if unknown:
            for clause in re.split(r"[.!\n]+", _own_text(response.reply_text)):
                if ("?" not in clause and re.search(r"\b(?:ось|вот|це|это|here\s+(?:are|is)|these\s+are|this\s+is)\b", clause, re.I)
                    and re.search(r"категор\w*|тем\w*|каталог\w*|модел\w*|принт\w*|categor\w*|themes?|catalog\w*|models?|prints?|hoodies?|t[ -]?shirts?|худі|худи|футбол\w*", clause, re.I)):
                    return "catalog_browse_unresolved_claim"
        return ""
    ids = parse_product_ids(raw)
    if not ids or not set(ids).issubset(capture["eligible_product_ids"]):
        return "catalog_browse_product_mismatch"
    shown_facts = [row for row in capture.get("product_facts", ()) if row["product_id"] in ids]
    text = _own_text(response.reply_text)
    body_garments = {garment for garment, pattern in _GARMENTS.items()
        if any(not _negative(text, match.start()) for match in pattern.finditer(text))}
    if body_garments and not body_garments.issubset({row["garment_type"] for row in shown_facts}):
        return "catalog_browse_body_mismatch"
    return ""


def browse_presentation_covered(capture, response, source_message_id=None):
    """A typed relevant presentation covers only its own source request."""
    from management.services.ig_catalog_media import parse_product_ids
    if not valid_capture(capture) or validate_catalog_browse_response(capture, response):
        return False
    shown = set(parse_product_ids(response.control.get("show_products")) or ())
    requests = [r for r in capture.get("requests", ()) if source_message_id is None or r["source_id"] == source_message_id]
    return bool(requests and shown and all(r["status"] == "resolved" and shown & set(r["eligible_product_ids"]) for r in requests))


def browse_clarification_covered(capture, response, source_message_id=None):
    """One subject clarification can resolve an unrecognized browse request."""
    if not valid_capture(capture) or response.control:
        return False
    scope_ids = capture.get("scope", {}).get("source_ids", ())
    if not scope_ids or (source_message_id is not None and source_message_id not in scope_ids):
        return False
    requests = [row for row in capture.get("requests", ())
        if source_message_id is None or row["source_id"] == source_message_id]
    empty_published = (capture.get("complete") is True and requests
        and all(row["status"] == "resolved" and not row["eligible_product_ids"] for row in requests))
    unknown = (capture.get("status") == "unavailable" and not capture.get("complete")) or (
        requests and all(row["status"] == "unresolved" for row in requests))
    if not unknown and not empty_published:
        return False
    text = _own_text(response.reply_text).strip()
    # A sole question carries no invented factual answer, custom-print offer,
    # product selection or consent. A size/payment question is not this debt.
    return bool(re.fullmatch(r"[^.!?]+\?", text)
        and re.search(r"\b(?:яку|який|яка|які|що\s+саме|какую|какой|какая|какие|что\s+именно|which|what)\b", text, re.I)
        and re.search(r"\b(?:тем\w*|категор\w*|дизайн\w*|принт\w*|тип\w*\s+одягу|вид\w*\s+одежды|themes?|categor\w*|designs?|garment\s+type|print\s+theme)\b", text, re.I))


def browse_prompt_guidance(capture):
    """Small explicit projection; raw customer strings are not new instructions."""
    if not capture:
        return {}
    ids = list(capture.get("eligible_product_ids") or ())[:3]
    requests = [{key: deepcopy(row[key]) for key in ("source_id", "garments", "excluded_garments", "include", "exclude", "mode", "status", "clarification_reason")}
        | {"eligible_count": len(row["eligible_product_ids"])} for row in capture.get("requests", ())]
    return {"schema": SCHEMA, "status": capture.get("status"), "complete": capture.get("complete", False),
        "reason": capture.get("reason", ""), "requests": requests,
        "eligible_count": len(capture.get("eligible_product_ids") or ()),
        "shortlist": [deepcopy(row) for row in capture.get("product_facts", ()) if row["product_id"] in ids],
        "navigation": {str(pk): deepcopy(capture["navigation"][str(pk)]) for pk in ids if str(pk) in capture.get("navigation", {})}}


def browse_authorized_urls(capture):
    """Only the current captured supported navigation can authorize prose URLs."""
    if not valid_capture(capture) or capture.get("status") != "captured":
        return ()
    urls = []
    for product_id, nav in capture.get("navigation", {}).items():
        if not product_id.isdecimal() or int(product_id) not in capture["eligible_product_ids"]:
            return ()
        try:
            url = navigation_url(nav)
        except (ValueError, TypeError, KeyError):
            return ()
        if url not in urls:
            urls.append(url)
    return tuple(urls)
