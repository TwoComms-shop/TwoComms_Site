"""Advertising is an entry source, not proof of a chosen product or purchase."""
from copy import deepcopy
from types import SimpleNamespace
from django.db import DatabaseError
from management.models import IgFunnelResetAudit, IgTurnRevisionSource
from management.services.ig_ad_referral import resolve_ad_referral


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
        if not ref:
            return result
        resolution = resolve_ad_referral(SimpleNamespace(ad_id=ad_id, ad_ref=ref))
        if resolution.status != "resolved":
            return result
    refs = [{"kind": "client", "id": client.pk}]
    attribution_scope = "client"
    try:
        reset = IgFunnelResetAudit.objects.filter(client_id=client.pk).order_by("-pk").values("reset_after_message_id").first()
        if reset:
            sources = list(IgTurnRevisionSource.objects.filter(
                message__client_id=client.pk, role="user",
                message_id__gt=int(reset["reset_after_message_id"] or 0),
            ).exclude(referral={}).exclude(message__status="failed")
                .order_by("-message_id", "-pk").values("message_id", "referral")[:32])
            matching = [r for r in sources if isinstance(r["referral"], dict)
                        and (r["referral"].get("ad_id") or str(r["referral"].get("source", "")).upper() in {"ADS", "AD", "ADVERTISEMENT"}
                             or isinstance(r["referral"].get("ads_context_data"), dict) and bool(r["referral"]["ads_context_data"]))]
            if not matching:
                return result
            payload = matching[0]["referral"]
            ad_id, ref = str(payload.get("ad_id") or "")[:64], str(payload.get("ref") or "")[:255]
            refs = [{"kind": "message", "id": matching[0]["message_id"]}]
            attribution_scope = "message_after_reset"
            resolution = None
    except DatabaseError:
        return result
    resolution = resolution or resolve_ad_referral(SimpleNamespace(ad_id=ad_id, ad_ref=ref))
    acd = payload.get("ads_context_data") if isinstance(payload.get("ads_context_data"), dict) else {}
    title = str(acd.get("ad_title") or (getattr(client, "ad_title", "") if attribution_scope == "client" else ""))[:255]
    campaign = resolution.campaign if resolution.status == "resolved" else None
    product_id = resolution.product_id if campaign else None
    inbound_nodes = [n for n in result["nodes"] if n.get("semantic_key") == "inbound"]
    for node in inbound_nodes:
        if node.get("semantic_key") != "inbound":
            continue
        node["ad_entry"] = {"source": "advertising", "scope": attribution_scope,
            "resolution": resolution.status, "product_id": product_id, "evidence_refs": refs,
            "ad_id": ad_id, "ref": ref, "title": title or str(getattr(campaign, "title", "") or ""),
            "product_title": str(campaign.product.title) if product_id else "",
            "theme": str(getattr(campaign, "theme", "") or ""),
            "intent_status": "not_inferred", "mapping_id": getattr(campaign, "pk", None)}
        node["label"] = "Звернення"
        node["short_label"] = "Звернення"
        facts = [f for f in node.get("facts", []) if not str(f.get("id", "")).startswith("ad-entry:")]
        facts.append({"id": "ad-entry:source", "label": "Джерело", "value": "Реклама Instagram",
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
        ad_node = {"id": node_id, "semantic_key": "advertising_entry", "label": "З реклами",
                   "short_label": "З реклами", "state": "partial", "current": False,
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
