"""Safe Meta CAPI feedback hooks for IG Direct funnel events.

The default behavior is audit-only. We never send IG-only lead stages unless the
explicit management flag is enabled and there is enough match data or an order.
"""
from __future__ import annotations

import uuid

from django.conf import settings
from django.utils import timezone

from management.models import IgMetaEventLog, InstagramBotSettings


def _has_capi_env() -> bool:
    return bool(
        getattr(settings, "META_PIXEL_ID", None)
        and getattr(settings, "FACEBOOK_CONVERSIONS_API_TOKEN", None)
    )


def _has_match_data(client) -> bool:
    return bool(getattr(client, "phone_normalized", "") or getattr(client, "phone", ""))


def log_or_send(event_name: str, *, client=None, deal=None, order=None, reason: str = "") -> IgMetaEventLog:
    settings_obj = InstagramBotSettings.load()
    event_id = f"ig-{event_name.lower()}-{uuid.uuid4().hex[:16]}"
    if not settings_obj.meta_feedback_enabled:
        return IgMetaEventLog.objects.create(
            event_name=event_name,
            event_id=event_id,
            client=client,
            deal=deal,
            order=order,
            status=IgMetaEventLog.Status.DISABLED,
            reason=reason or "meta_feedback_disabled",
        )
    if not _has_capi_env():
        return IgMetaEventLog.objects.create(
            event_name=event_name,
            event_id=event_id,
            client=client,
            deal=deal,
            order=order,
            status=IgMetaEventLog.Status.SKIPPED,
            reason="skipped_no_capi_env",
        )
    if not order and not _has_match_data(client):
        return IgMetaEventLog.objects.create(
            event_name=event_name,
            event_id=event_id,
            client=client,
            deal=deal,
            order=order,
            status=IgMetaEventLog.Status.SKIPPED,
            reason="skipped_no_match_data",
        )

    if order:
        try:
            from orders.facebook_conversions_service import FacebookConversionsService

            service = FacebookConversionsService()
            if not service.enabled:
                raise RuntimeError("facebook_capi_service_disabled")
            normalized_event = event_name.lower()
            if normalized_event == "purchase":
                event_id = order.get_purchase_event_id()
                payment_payload = getattr(order, "payment_payload", None) or {}
                facebook_events = payment_payload.get("facebook_events", {})
                if facebook_events.get("purchase_sent"):
                    return IgMetaEventLog.objects.create(
                        event_name=event_name,
                        event_id=event_id,
                        client=client,
                        deal=deal,
                        order=order,
                        status=IgMetaEventLog.Status.SENT,
                        reason="already_sent_by_retail_payment_flow",
                    )
                ok = service.send_purchase_event(order)
                if ok:
                    facebook_events["purchase_sent"] = True
                    facebook_events["purchase_sent_at"] = timezone.now().isoformat()
                    payment_payload["facebook_events"] = facebook_events
                    order.payment_payload = payment_payload
                    order.save(update_fields=["payment_payload"])
            elif normalized_event == "lead":
                event_id = order.get_lead_event_id()
                ok = service.send_lead_event(order)
            else:
                ok = False
            return IgMetaEventLog.objects.create(
                event_name=event_name,
                event_id=event_id,
                client=client,
                deal=deal,
                order=order,
                status=IgMetaEventLog.Status.SENT if ok else IgMetaEventLog.Status.FAILED,
                reason="" if ok else "sdk_rejected_or_unavailable",
            )
        except Exception as exc:
            return IgMetaEventLog.objects.create(
                event_name=event_name,
                event_id=event_id,
                client=client,
                deal=deal,
                order=order,
                status=IgMetaEventLog.Status.FAILED,
                reason=repr(exc)[:255],
            )

    return IgMetaEventLog.objects.create(
        event_name=event_name,
        event_id=event_id,
        client=client,
        deal=deal,
        order=order,
        status=IgMetaEventLog.Status.SKIPPED,
        reason=reason or "skipped_no_order_event_mapping",
    )
