"""Backstop-поллінг статусів оплати угод IG-бота (якщо вебхук Monobank не дійшов).

Запуск кроном кожні кілька хвилин:
    python manage.py poll_ig_deal_payments
"""
from django.core.management.base import BaseCommand

from management.services import bot_payments


class Command(BaseCommand):
    help = "Поллінг угод IG-бота у статусі awaiting_payment (pull-verify Monobank)."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=50)

    def handle(self, *args, **opts):
        reconciled = bot_payments.reconcile_payment_projections(limit=opts.get("limit") or 50)
        paid = bot_payments.poll_pending_deals(limit=opts.get("limit") or 50)
        # Safety-net: дотворюємо замовлення для оплачених угод з повними даними НП,
        # якщо модель не виставила тег [ORDER].
        from management.services import bot_orders

        fulfilled = bot_orders.fulfill_ready_paid_deals(limit=opts.get("limit") or 50)
        shipped = bot_orders.notify_shipped_deals(limit=opts.get("limit") or 50)
        self.stdout.write(
            self.style.SUCCESS(
                f"Звірено проєкцій: {reconciled}; Оплачено угод за цей прогін: {paid}; "
                f"дотворено замовлень: {fulfilled}; "
                f"сповіщень про відправку: {shipped}"
            )
        )
