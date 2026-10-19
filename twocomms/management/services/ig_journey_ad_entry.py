"""Advertising is an entry source, not proof of a chosen product or purchase."""
from copy import deepcopy
from types import SimpleNamespace
from django.db import DatabaseError
from management.models import IgFunnelResetAudit, IgTurnRevisionSource
from management.services.ig_ad_referral import resolve_ad_referral


def _captured_ad_context(client, reset):
    """Read used candidate IDs/version hashes; never rebuild old prompt text."""
    from management.models import GeminiRequest, InstagramBotMessage
    from management.services.ig_request_manifest import sanitize_request_context

    floor = int(reset["reset_after_message_id"] or 0) + 1 if reset else 1
    rows = GeminiRequest.objects.filter(client_id=client.pk, source_message_id__gte=floor).order_by("-pk").values("policy_manifest")[:24]
    for row in rows:
        policy = row.get("policy_manifest") or {}
        context = policy.get("request_context") if isinstance(policy, dict) else None
        if not isinstance(context, dict) or not context.get("ad_context_snapshot"):
            continue
        try:
            context = sanitize_request_context(context)
        except (ValueError, TypeError):
            continue
        ad = context.get("ad_context_snapshot") or {}
        touches = ad.get("touches") or []
        ids = [touch["source_message_id"] for touch in touches]
        if not ids or context.get("client_id") != client.pk or any(mid < floor for mid in ids):
            continue
        source_ids = context.get("source_message_ids") or []
        if not set(ids).issubset(source_ids):
            continue
        owned = set(InstagramBotMessage.objects.filter(client_id=client.pk, role="user", pk__in=ids)
            .exclude(status="failed").values_list("pk", flat=True))
        if owned != set(ids):
            continue
        return ad
    return None


def _source_context(client, reset):
    """A bounded current-scope source read; each touch keeps its own MID."""
    from management.services.ig_ad_context import capture_ad_context

    floor = int(reset["reset_after_message_id"] or 0) if reset else 0
    rows = list(IgTurnRevisionSource.objects.filter(message__client_id=client.pk, role="user",
        message_id__gt=floor).exclude(referral={}).exclude(message__status="failed")
        .order_by("-message_id", "-pk").values("message_id", "referral", "source_namespace", "provider_message_id", "provider_created_at")[:32])
    sources, seen = [], set()
    for row in reversed(rows):
        if row["message_id"] in seen:
            continue
        ref = row["referral"]
        if not isinstance(ref, dict) or not any(ref.get(key) for key in ("ad_id", "ref", "source", "ads_context_data")):
            continue
        sources.append(row)
        seen.add(row["message_id"])
    if not sources:
        return None
    captured = capture_ad_context(sources, {"reset_floor": floor + 1})
    captured["observation"] = "current_mapping_at_map_not_original_request"
    captured["historical_request_revision"] = "unavailable"
    return captured


def _standalone_context(client, reset):
    from management.models import IgWebhookInboxEvent
    from management.services.ig_webhook_inbox import _row_event
    from management.services.ig_ad_context import sanitize_referral

    rows = IgWebhookInboxEvent.objects.filter(customer_igsid=client.igsid,
        reason="attribution_only", decision="accepted")
    if reset:
        rows = rows.filter(received_at__gt=reset["created_at"])
    for row in rows.order_by("-pk")[:8]:
        if row.namespace != "instagram_login:" + row.owner_id and row.namespace != "facebook_login:" + row.owner_id:
            continue
        event = _row_event(row.payload)
        if str((event.get("sender") or {}).get("id") or "") != client.igsid or str((event.get("recipient") or {}).get("id") or "") != row.owner_id:
            continue
        return dict(referral=sanitize_referral(event.get("referral") or {}), event_id=row.pk,
            namespace=row.namespace, event_at=event.get("timestamp"), payload_digest=row.payload_digest)
    return None



def append_ad_entry(graph, *, client, is_history):
    result = deepcopy(graph)
    if is_history or getattr(client, "privacy_erasure_started_at", None):
        return result
    payload = getattr(client, "referral_payload", {}) or {}
    if not isinstance(payload, dict):
        payload = {}
    ad_id = str(getattr(client, "ad_id", "") or payload.get("ad_id") or "")[:64]
    ref = str(getattr(client, "ad_ref", "") or payload.get("ref") or "")[:255]
    source = str(getattr(client, "ad_source", "") or payload.get("source") or "").upper()
    context = payload.get("ads_context_data") if isinstance(payload.get("ads_context_data"), dict) else {}
    resolution = None
    if not ad_id and source not in {"ADS", "AD", "ADVERTISEMENT"} and not context and not getattr(client, "ad_title", ""):
        if ref:
            resolution = resolve_ad_referral(SimpleNamespace(ad_id=ad_id, ad_ref=ref))
    refs = [{"kind": "client", "id": client.pk}]
    attribution_scope = "client"
    snapshot = None
    standalone = None
    try:
        reset = IgFunnelResetAudit.objects.filter(client_id=client.pk).order_by("-pk").values("reset_after_message_id", "created_at").first()
        snapshot = _captured_ad_context(client, reset)
        if snapshot:
            attribution_scope = "captured_request"
            refs = [{"kind": "message", "id": row["source_message_id"]} for row in snapshot["touches"]]
            used = IgTurnRevisionSource.objects.filter(message_id=snapshot["touches"][-1]["source_message_id"], message__client_id=client.pk).order_by("pk").values("referral", "source_namespace").first()
            payload = used["referral"] if used and isinstance(used["referral"], dict) else {}
            event_ids = [row["referral_event_id"] for row in snapshot["touches"] if row.get("referral_event_id")]
            refs.extend({"kind": "webhook_event", "id": event_id} for event_id in event_ids)
            if snapshot["touches"][-1].get("referral_event_id") and used:
                from management.models import IgWebhookInboxEvent
                from management.services.ig_webhook_inbox import _row_event
                from management.services.ig_ad_context import digest
                receipt = IgWebhookInboxEvent.objects.filter(pk=snapshot["touches"][-1]["referral_event_id"],
                    namespace=used["source_namespace"], customer_igsid=client.igsid,
                    decision="accepted", reason="attribution_only").first()
                if receipt and digest(receipt.payload) == receipt.payload_digest:
                    payload = _row_event(receipt.payload).get("referral") or {}
            ad_id, ref = str(payload.get("ad_id") or "")[:64], str(payload.get("ref") or "")[:255]
        else:
            snapshot = _source_context(client, reset)
            if snapshot:
                attribution_scope = "message_after_reset" if reset else "message"
                refs = [{"kind": "message", "id": row["source_message_id"]} for row in snapshot["touches"]]
                payload = snapshot["touches"][-1]["referral"]
                ad_id, ref = str(payload.get("ad_id") or "")[:64], str(payload.get("ref") or "")[:255]
        standalone = _standalone_context(client, reset)
        if not snapshot:
            if standalone:
                attribution_scope = "standalone_attribution"
                payload = standalone["referral"]
                ad_id, ref = str(payload.get("ad_id") or "")[:64], str(payload.get("ref") or "")[:255]
                refs = [{"kind": "webhook_event", "id": standalone["event_id"]}]
            elif reset:
                return result
    except DatabaseError:
        return result
    if snapshot:
        touches = snapshot.get("touches") or []
        candidates = snapshot.get("candidates") or [dict(candidate, source_message_id=touch["source_message_id"])
            for touch in touches for candidate in touch.get("candidates", [])]
        pids = {candidate["product_id"] for candidate in candidates}
        # Product display is current; original request IDs/digests remain sealed.
        from storefront.models import Product
        titles = dict(Product.objects.filter(pk__in=pids).values_list("pk", "title"))
        pid = next(iter(pids)) if snapshot.get("status") == "resolved" and len(pids) == 1 else None
        theme = snapshot.get("theme") or (touches[-1].get("theme", "") if touches else "")
        campaign = SimpleNamespace(pk=touches[-1].get("mapping_id") if len(touches) == 1 else None,
            product_id=pid, product=SimpleNamespace(title=titles.get(pid, f"#{pid}")) if pid else None, theme=theme, title="")
        from management.services.ig_ad_referral import AdReferralResolution
        reasons = tuple(dict.fromkeys(reason for touch in touches for reason in touch.get("reason_codes", [])))
        resolution = AdReferralResolution(snapshot.get("status", "unavailable"), reasons, campaign=campaign,
            context=dict(candidates=candidates))
    else:
        resolution = resolution or resolve_ad_referral(SimpleNamespace(ad_id=ad_id, ad_ref=ref, referral_payload=payload))
    if not snapshot and not standalone and not ad_id and source not in {"ADS", "AD", "ADVERTISEMENT"} and not context and not getattr(client, "ad_title", "") and (not ref or resolution.status != "resolved"):
        return result
    acd = payload.get("ads_context_data") if isinstance(payload.get("ads_context_data"), dict) else {}
    title = str(acd.get("ad_title") or (getattr(client, "ad_title", "") if attribution_scope == "client" else ""))[:255]
    campaign = resolution.campaign if resolution.status == "resolved" else None
    product_id = resolution.product_id if campaign else None
    source_class = "advertising" if ad_id or source in {"ADS", "AD", "ADVERTISEMENT"} or resolution.status == "resolved" else "referral"
    inbound_nodes = [n for n in result["nodes"] if n.get("semantic_key") == "inbound"]
    for node in inbound_nodes:
        if node.get("semantic_key") != "inbound":
            continue
        node["ad_entry"] = {"source": source_class, "scope": attribution_scope,
            "resolution": resolution.status, "product_id": product_id, "evidence_refs": refs,
            "ad_id": ad_id, "ref": ref, "title": title or str(getattr(campaign, "title", "") or ""),
            "product_title": str(campaign.product.title) if product_id else "",
            "theme": str(getattr(campaign, "theme", "") or ""),
            "intent_status": "not_inferred", "selection_status": "not_selected", "mapping_id": getattr(campaign, "pk", None),
            "reason_codes": list(resolution.reason_codes), "candidates": deepcopy(snapshot.get("candidates") or candidates) if snapshot else resolution.snapshot().get("candidates", []),
            "touches": deepcopy(snapshot.get("touches", [])) if snapshot else [],
            "context_digest": snapshot.get("context_digest", "") if snapshot else "",
            "observation": "captured_request" if attribution_scope == "captured_request" else "current_mapping_at_map_not_original_request",
            "original_creative_reconstruction": "not_retained",
            "standalone_event": deepcopy(standalone) if standalone else None}
        node["label"] = "Звернення"
        node["short_label"] = "Звернення"
        facts = [f for f in node.get("facts", []) if not str(f.get("id", "")).startswith("ad-entry:")]
        facts.append({"id": "ad-entry:source", "label": "Джерело", "value": "Реклама Instagram" if source_class == "advertising" else "Перехід до Instagram Direct",
                      "state": "complete", "source": "stored_referral", "evidence_refs": refs})
        if ad_id or ref:
            facts.append({"id": "ad-entry:identity", "label": "Оголошення · ідентифікатор", "value": ad_id or ref,
                          "state": "complete", "source": "stored_referral", "evidence_refs": refs})
        if title:
            facts.append({"id": "ad-entry:title", "label": "Оголошення", "value": title,
                          "state": "partial", "source": "stored_referral", "evidence_refs": refs})
        if product_id:
            facts.append({"id": "ad-entry:product", "label": "Товар реклами",
                          "value": campaign.product.title, "state": "complete", "source": "ad_campaign_mapping",
                          "evidence_refs": [{"kind": "product", "id": product_id}]})
        elif node["ad_entry"]["candidates"]:
            rows = node["ad_entry"]["candidates"]
            from storefront.models import Product
            display = dict(Product.objects.filter(pk__in=[row["product_id"] for row in rows]).values_list("pk", "title"))
            facts.append({"id": "ad-entry:candidates", "label": "Кандидати реклами", "state": "partial",
                "value": "; ".join((str(row["position"]) + ": " if row.get("position") else "") + display.get(row["product_id"], "#" + str(row["product_id"])) for row in rows),
                "source": "captured_ad_candidates" if attribution_scope == "captured_request" else "ad_campaign_mapping",
                "evidence_refs": refs})
        elif campaign and campaign.theme:
            facts.append({"id": "ad-entry:theme", "label": "Тема реклами", "value": campaign.theme,
                          "state": "partial", "source": "ad_campaign_mapping", "evidence_refs": refs})
        else:
            facts.append({"id": "ad-entry:unresolved", "label": "Товар",
                          "value": "Рекламний вхід підтверджено; товар потрібно уточнити.",
                          "state": "partial", "source": "stored_referral", "evidence_refs": refs})
        node["facts"] = facts
    if inbound_nodes:
        origin = inbound_nodes[0]
        node_id = f"guide:ad-entry:{client.pk}"
        ad_node = {"id": node_id, "semantic_key": "advertising_entry", "label": "З реклами" if source_class == "advertising" else "З посилання",
                   "short_label": "З реклами" if source_class == "advertising" else "З посилання", "state": "partial", "current": False,
                   "presentation_kind": "client_context", "producer": "advertising_attribution",
                   "scope": "client", "episode_id": None, "ad_entry": deepcopy(origin["ad_entry"]),
                   "facts": [deepcopy(f) for f in origin["facts"] if str(f.get("id", "")).startswith("ad-entry:")],
                   "evidence_refs": refs, "timers": [], "summary": "Джерело звернення. Товар реклами не дорівнює підтвердженому вибору клієнта; намір визначається з діалогу."}
        result["nodes"] = [n for n in result["nodes"] if n["id"] != node_id] + [ad_node]
        edge_id = f"ad-attribution:{client.pk}"
        result["edges"] = [e for e in result.get("edges", []) if e["id"] != edge_id] + [{
            "id": edge_id, "from_node_id": origin["id"], "to_node_id": node_id,
            "relation": "advertising_attribution", "condition_label": "Рекламний вхід", "evidence_refs": refs}]
    return result
