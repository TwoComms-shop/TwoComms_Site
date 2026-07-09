"""Seed routed Instagram bot sales playbooks."""
from django.core.management.base import BaseCommand

from management.models import BotInstruction


PLAYBOOKS = [
    {
        "title": "IG Core Sales",
        "intent_tags": "global,core,sales",
        "priority": 10,
        "body": (
            "Веди клієнта до наступного кроку без тиску: зрозумій товар, розмір, колір, "
            "кількість, для себе чи на подарунок. Пиши коротко, мовою клієнта. Не вигадуй "
            "ціни/наявність/посилання; якщо не впевнений у товарі, попроси посилання на пост "
            "або уточнення."
        ),
    },
    {
        "title": "Product / SKU Context",
        "intent_tags": "product,catalog,product_matched,checkout",
        "priority": 20,
        "body": (
            "Якщо товар визначений, тримай саме його як основний SKU діалогу. Уточнюй тільки "
            "потрібні для оформлення речі: розмір, колір, кількість, тип оплати і доставку."
        ),
    },
    {
        "title": "Size And Fit",
        "intent_tags": "size,fit",
        "priority": 30,
        "body": (
            "На питання про розмір пояснюй різницю regular/oversize і пропонуй розмірну сітку "
            "або допомогу по замірах. Не гарантуй посадку без даних з розмірної сітки."
        ),
    },
    {
        "title": "Prepayment Objection",
        "intent_tags": "prepayment,payment",
        "priority": 40,
        "body": (
            "Передоплата 200 грн потрібна як страховка, щоб бренд не працював у мінус, якщо "
            "посилку не заберуть або трапиться форс-мажор. Пояснюй це спокійно: основна сума "
            "доплачується при отриманні, а розміри/деталі узгоджуємо заздалегідь."
        ),
    },
    {
        "title": "Price Objection / Rescue",
        "intent_tags": "price,discount",
        "priority": 50,
        "body": (
            "Спочатку відпрацьовуй цінність: власне виробництво, якісна тканина, DTF-друк, "
            "відгуки. Не пропонуй знижку самостійно: автоматична система окремо дасть 5%, "
            "а 10% тільки як фінальний або явно узгоджений варіант."
        ),
    },
    {
        "title": "Custom Print Handoff",
        "intent_tags": "custom_print",
        "priority": 60,
        "body": (
            "Для кастомного принта не називай фінальну ціну. Коротко поясни: можемо зробити "
            "майже будь-який DTF-принт, ціна залежить від крою, розміру принта і готовності "
            "файлу. Збери базове ТЗ і переведи в Telegram менеджера з шапки профілю."
        ),
    },
    {
        "title": "Stop / No-buy",
        "intent_tags": "no_buy,stop,cold,spam",
        "priority": 70,
        "body": (
            "Якщо клієнт відмовився, просить не писати або це спам, не тисни. Один раз чемно "
            "закрий діалог і не став follow-up. Можна запросити стежити за майбутніми дропами."
        ),
    },
]


class Command(BaseCommand):
    help = "Seed routed sales playbooks for the Instagram Direct bot."

    def handle(self, *args, **options):
        created = 0
        updated = 0
        for item in PLAYBOOKS:
            obj, was_created = BotInstruction.objects.update_or_create(
                title=item["title"],
                defaults={
                    "body": item["body"],
                    "intent_tags": item["intent_tags"],
                    "priority": item["priority"],
                    "is_active": True,
                },
            )
            if was_created:
                created += 1
            else:
                updated += 1
        self.stdout.write(self.style.SUCCESS(f"IG sales playbooks seeded: {created} created, {updated} updated."))
