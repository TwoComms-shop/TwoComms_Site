"""Dry-run-first, evidence-bound repair of one manually created IG order."""

from __future__ import annotations

import json

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from management.models import IgClient
from management.models import InstagramBotMessage
from orders.models import Order


class Command(BaseCommand):
    help = "Repair one paid Instagram order through payment review, attribution and episode binding."

    def add_arguments(self, parser):
        parser.add_argument("--client-id", type=int, required=True)
        parser.add_argument("--order-number", required=True)
        parser.add_argument("--evidence-message-id", type=int, required=True)
        parser.add_argument("--actor-id", type=int, required=True)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        client = IgClient.objects.filter(pk=options["client_id"], hidden_at__isnull=True).first()
        if client is None:
            raise CommandError("Instagram-клієнта не знайдено.")
        order = Order.objects.filter(order_number=str(options["order_number"]).strip()).first()
        if order is None:
            raise CommandError("Замовлення з таким точним номером не знайдено.")
        source = str(getattr(order, "source", "") or "").casefold()
        sale_source = str(getattr(order, "sale_source", "") or "").casefold()
        if source != "manual" or sale_source != "instagram":
            raise CommandError("Замовлення не є ручним Instagram-замовленням.")
        evidence_message_id = int(options["evidence_message_id"])
        evidence_row = InstagramBotMessage.objects.filter(
            client_id=client.pk,
            pk=evidence_message_id,
        ).first()
        if evidence_row is None:
            raise CommandError("Повідомлення-доказ не належить цьому Instagram-клієнту.")
        actor = get_user_model().objects.filter(pk=options["actor_id"]).first()
        if not actor or not (getattr(actor, "is_staff", False) or getattr(actor, "is_superuser", False)):
            raise CommandError("--actor-id має посилатися на staff-користувача.")

        result = {
            "client_id": client.pk,
            "order_number": order.order_number,
            "evidence_message_id": evidence_message_id,
            "dry_run": not options["apply"],
        }
        if not options["apply"]:
            paid = str(order.payment_status or "").casefold() in {"paid", "prepaid", "partial"}
            result["status"] = "eligible" if paid else "blocked"
            result["reason"] = (
                "apply_required_for_audited_mutation"
                if paid else "order_payment_status_not_reconcilable"
            )
            result["evidence_role"] = evidence_row.role
            self.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return

        from management.services.ig_manual_order_reconciliation import reconcile_manager_paid_order

        try:
            review, linked = reconcile_manager_paid_order(
                client=client,
                order=order,
                actor=actor,
                evidence_message_id=evidence_message_id,
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        result.update({
            "dry_run": False,
            "status": "applied",
            "review_id": review.pk,
            "order_id": linked.pk,
        })
        self.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True))
