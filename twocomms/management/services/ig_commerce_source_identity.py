"""Original inbound identity resolution; presentation receipts are evidence only."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import re

from management.services.ig_commerce_types import ProductReference


def _normalize(value):
    return " ".join(re.findall(r"[\w]+", str(value or "").casefold()))


def named_product_ids(text, products):
    """Match complete reviewed names/aliases, never a similarity score."""
    normalized = " " + _normalize(text) + " "
    result = set()
    for product in products:
        aliases = [product.title]
        aliases.extend(alias for values in product.aliases.values() for alias in values)
        for alias in aliases:
            needle = _normalize(alias)
            # Single letter/numeric titles are not meaningful inbound identity.
            if len(needle) < 3 or needle.isdigit():
                continue
            match = re.search(r"(?<!\w)" + re.escape(needle) + r"(?!\w)", normalized)
            if match and not re.search(r"\b(?:не|ні|not|no)(?:\s+(?:хочу|want))?\s*$", normalized[:match.start()]):
                result.add(product.product_id)
    return tuple(sorted(result))


def _named_alias_spans(text, products, product_ids):
    raw = str(text or "")
    for product in products:
        if product.product_id not in product_ids:
            continue
        aliases = [product.title, *(alias for values in product.aliases.values() for alias in values)]
        for alias in aliases:
            tokens = re.findall(r"[\w]+", str(alias or ""))
            if len(_normalize(alias)) < 3 or not tokens or _normalize(alias).isdigit():
                continue
            pattern = r"(?<!\w)" + r"\W+".join(re.escape(token) for token in tokens) + r"(?!\w)"
            yield from re.finditer(pattern, raw, re.I)


def _named_clause_is_selection(raw):
    """A question/report about a name is not a request to change identity."""
    from management.services.ig_commerce_turns import _quoted_preference_spans

    raw = str(raw or "")
    lowered = raw.casefold()
    if "?" in raw:
        return False
    if re.search(r"\b(?:do|does|did|have|has|had)n['’]t\s+(?:want|need|choose|select|order|buy|request(?:ed)?|chosen|selected|ordered|said|asked\s+for)\b", lowered):
        return False
    reported = bool(re.search(r"написан\w*|написав\w*|писа[вл]\w*|said|wrote|reported|вказан\w*|(?:у|в)\s+реклам\w*|опис[аі]\w*|description|caption|advert\w*|назв[ао]\w*|назван\w*", lowered))
    spans = tuple(_quoted_preference_spans(raw))
    quoted = bool(spans)
    if not (reported or quoted):
        return True
    outside = list(raw)
    for match in spans:
        outside[match.start():match.end()] = " " * (match.end() - match.start())
    affirmative = bool(re.search(r"\b(?:беру|обираю|выбираю|замовляю|заказываю|купую|choose|select|хочу|want)\b", "".join(outside).casefold()))
    return affirmative and not reported


def _named_identity_is_selection(text, products, product_ids):
    """Scope identity evidence to its clause, retaining its question delimiter."""
    raw = str(text or "")
    for match in _named_alias_spans(raw, products, product_ids):
        start = max(raw.rfind(marker, 0, match.start()) for marker in ".!?;\n") + 1
        ends = [index for marker in ".!?;\n" if (index := raw.find(marker, match.end())) >= 0]
        end = min(ends) + 1 if ends else len(raw)
        if _named_clause_is_selection(raw[start:end]):
            return True
    return False


_CONFIGURATION_MARKERS = {"fit": r"fit|cut|крій|крой|посадк\w*", "color": r"colou?r|колір|цвет"}


def _alias_has_configuration_marker(raw, match, field):
    before, after = raw[:match.start()], raw[match.end():]
    return bool(re.search(r"\b(?:" + _CONFIGURATION_MARKERS[field] + r")\s*[:=-]?\s*$", before, re.I)
        or re.match(r"\s*(?:" + _CONFIGURATION_MARKERS[field] + r")\b", after, re.I))


def _without_title_configuration(request, text, products, product_ids):
    """Names such as Classic identify models unless a fit/color marker binds them."""
    from management.services.ig_commerce_turns import _COLOR_WORDS, _FIT_WORDS, _find_prefix_value

    raw = str(text or "")
    spans = tuple(_named_alias_spans(raw, products, product_ids))
    updates = dict(request.field_updates)
    for field, words in (("fit", _FIT_WORDS), ("color", _COLOR_WORDS)):
        masked = list(raw)
        for match in spans:
            if not _alias_has_configuration_marker(raw, match, field):
                masked[match.start():match.end()] = " " * (match.end() - match.start())
        value = _find_prefix_value("".join(masked), words)
        if value:
            updates[field] = value
        else:
            updates.pop(field, None)
    return replace(request, field_updates=updates)


def _current_presentations(client, source):
    """Bounded owned SENT set; UNKNOWN and incomplete presentation groups abstain."""
    from management.models import IgCommerceSelectionSession, IgCommerceTurnDecision, IgRevisionDeliveryEffect
    from management.services.ig_conversation_routes import conversation_route_reset_floor
    from management.services.ig_revision_outbox import _digest

    episode_id = client.current_commercial_episode_id
    session = IgCommerceSelectionSession.objects.filter(client_id=client.pk, open_slot=1, commercial_episode_id=episode_id).first()
    if not episode_id or session is None:
        return ()
    lines = session.lines or []
    index = int(session.active_index or 0)
    line_id = str(lines[index].get("line_id") or "") if 0 <= index < len(lines) and isinstance(lines[index], dict) else ""
    floor = conversation_route_reset_floor(client.pk)
    rows = list(IgRevisionDeliveryEffect.objects.filter(
        revision__client_id=client.pk, source_message_id__gte=floor,
        source_message_id__lt=source.pk, recipient_igsid=client.igsid,
        provider_namespace=source.provider_namespace, group="catalog_media",
    ).select_related("revision").order_by("-revision_id", "part_index")[:32])
    if not rows:
        return ()
    # A reply reference selects its original presentation, otherwise only the
    # latest presentation revision can supply current context.
    reply_to = str(source.reply_to_provider_message_id or "")
    matched = next((row for row in rows if row.provider_message_id == reply_to), None) if reply_to else rows[0]
    if matched is None:
        return ()
    group = [row for row in rows if row.revision_id == matched.revision_id]
    if not group or len(group) != matched.part_count or {row.part_index for row in group} != set(range(matched.part_count)):
        return ()
    admitted = ((matched.revision.action_receipts or {}).get("reply_projection_admission") or {}).get("funnel") or {}
    # Existing receipt projection owns the original episode, never current client
    # reconstruction. A foreign/stale binding cannot become a current selection.
    if (admitted.get("episode_id") != episode_id
        or admitted.get("revision_id") != matched.revision_id
        or admitted.get("client_id") != client.pk
        or admitted.get("snapshot_digest") != matched.revision_snapshot_digest
        or admitted.get("plan_digest") != matched.plan_digest
        or admitted.get("digest") != _digest({key: value for key, value in admitted.items() if key != "digest"})):
        return ()
    boundaries = list(session.transitions.filter(source_message_id__gt=matched.source_message_id).values("action", "reasons")[:64])
    if len(boundaries) == 64 or any(row["action"] in {"selection_reset", "product_rejected", "recipient_scope_changed"} or "recipient_scope_changed" in (row["reasons"] or []) for row in boundaries):
        return ()
    current_product = int(lines[index].get("product_id") or 0) if 0 <= index < len(lines) else 0
    shown_products = {int(row.projection_metadata.get("product_id") or 0) for row in group}
    if current_product and current_product not in shown_products:
        return ()
    for row in group:
        if (row.state != row.State.SENT or not row.provider_message_id
            or row.projection_digest != _digest(row.projection_metadata)
            or row.payload_digest != _digest(row.payload)
            or row.client_permission_epoch != client.reply_permission_epoch):
            return ()
        decision = IgCommerceTurnDecision.objects.filter(source_message_id=row.source_message_id, session=session).select_related("transition").first()
        if decision is None or decision.transition is None:
            return ()
        after = decision.transition.next_snapshot or {}
        prior = after.get("lines") or []
        prior_index = int(after.get("active_index") or 0)
        prior_line = prior[prior_index] if 0 <= prior_index < len(prior) and isinstance(prior[prior_index], dict) else {}
        if line_id and prior_line.get("line_id") not in (None, "", line_id):
            return ()
    return tuple(group)


def resolve_source_product_request(client, source, request, *, catalog_graph=None):
    """Resolve exact names or a customer's selection of one current presentation."""
    binding = {"source_message_id": source.pk, "source_digest": hashlib.sha256(str(source.text or "").encode()).hexdigest()}
    if request.exact_product_id:
        return replace(request, source_binding={**binding, "product_resolution": "exact_reference"})
    if request.reset_requested or request.new_purchase_requested or request.exchange_requested or request.rejected_product_ids:
        return replace(request, source_binding={**binding, "product_resolution": "unresolved"})
    if re.search(r"\b(?:не|ні|not|no)\s+(?:цю|эту|this)\b", str(source.text or ""), re.I):
        return replace(request, source_binding={**binding, "product_resolution": "unresolved"})
    from management.services.ig_catalog_graph import build_catalog_graph
    graph = catalog_graph if catalog_graph is not None else build_catalog_graph()
    named = named_product_ids(source.text, graph.products)
    if named:
        request = _without_title_configuration(request, source.text, graph.products, named)
        # An explicit "крій Classic" is a fit answer even if a catalog model
        # happens to share that word. A separate unmarked name still identifies it.
        named = tuple(product_id for product_id in named if any(
            not any(_alias_has_configuration_marker(source.text, match, field) for field in _CONFIGURATION_MARKERS)
            for match in _named_alias_spans(source.text, graph.products, (product_id,))))
    if named and not _named_identity_is_selection(source.text, graph.products, named):
        return replace(request, query=str(source.text or "")[:240], pending_clarification="which_product",
            source_binding={**binding, "product_resolution": "ambiguous", "candidate_product_ids": list(named)})
    if len(named) == 1:
        product = next(item for item in graph.products if item.product_id == named[0])
        if not request.garment_type or not product.garment_type or request.garment_type == product.garment_type:
            return replace(request, exact_product_id=named[0], exact_unique_alias=True,
                exact_reference=ProductReference(product_id=named[0], is_exact=True, reason="exact_source_name"),
                source_binding={**binding, "product_resolution": "exact_name"})
    if named:
        return replace(request, query=str(source.text or "")[:240], pending_clarification="which_product", source_binding={**binding, "product_resolution": "ambiguous", "candidate_product_ids": list(named)})
    choosing = request.purchase_requested or bool(request.field_updates.get("size")) or bool(re.search(
        r"\b(?:беру|обираю|выбираю|choose|select|this\s+one|цю|эту)\b", source.text, re.I))
    if choosing:
        presentations = _current_presentations(client, source)
        ids = {int(row.projection_metadata.get("product_id") or 0) for row in presentations}
        published = {item.product_id for item in graph.products}
        if len(ids) == 1 and ids <= published:
            product_id = next(iter(ids))
            return replace(request, exact_product_id=product_id,
                exact_reference=ProductReference(product_id=product_id, is_exact=True, reason="owned_sent_presentation"),
                source_binding={**binding, "product_resolution": "exact_presentation", "presentation_effect_ids": [row.pk for row in presentations]})
        return replace(request, pending_clarification="which_product", source_binding={**binding, "product_resolution": "ambiguous" if ids else "unresolved", "candidate_product_ids": sorted(ids)})
    return replace(request, source_binding={**binding, "product_resolution": "unresolved"})
