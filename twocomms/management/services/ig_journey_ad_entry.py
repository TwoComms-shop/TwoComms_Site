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
    if not ad_id and source not in {"ADS", "AD", "ADVERTISEMENT"}:
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
                        and (r["referral"].get("ad_id") or str(r["referral"].get("source", "")).upper() == "ADS")]
            if not matching:
                return result
            payload = matching[0]["referral"]
            ad_id, ref = str(payload.get("ad_id") or "")[:64], str(payload.get("ref") or "")[:255]
            refs = [{"kind": "message", "id": matching[0]["message_id"]}]
            attribution_scope = "message_after_reset"
    except DatabaseError:
        return result
    resolution = resolve_ad_referral(SimpleNamespace(ad_id=ad_id, ad_ref=ref))
    acd = payload.get("ads_context_data") if isinstance(payload.get("ads_context_data"), dict) else {}
    title = str(acd.get("ad_title") or (getattr(client, "ad_title", "") if attribution_scope == "client" else ""))[:255]
    campaign = resolution.campaign if resolution.status == "resolved" else None
    product_id = resolution.product_id if campaign else None
    for node in result["nodes"]:
        if node.get("semantic_key") != "inbound":
            continue
        node["ad_entry"] = {"source": "advertising", "scope": attribution_scope,
            "resolution": resolution.status, "product_id": product_id, "evidence_refs": refs}
        node["label"] = "Звернення з реклами"
        node["short_label"] = "З реклами"
        facts = [f for f in node.get("facts", []) if not str(f.get("id", "")).startswith("ad-entry:")]
        facts.append({"id": "ad-entry:source", "label": "Джерело", "value": "Реклама Instagram",
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
    return result
