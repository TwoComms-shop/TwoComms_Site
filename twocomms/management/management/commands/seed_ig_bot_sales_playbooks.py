"""Seed routed Instagram bot sales playbooks."""
import re
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from management.knowledge.publication_compat import REVIEWED_BODIES
from management.models import BotInstruction
from management.services.approved_public_facts import approved_provider_fact_text


_DISPATCH_POLICY = approved_provider_fact_text("dispatch")
_SERVICE_POLICY = approved_provider_fact_text("service_boundary")
_OBJHANDLE_VALUES = frozenset({
    "price:value_breakdown",
    "thinking:soft_isolate",
    "size_risk:size_consult",
    "size_risk:risk_reversal_exchange",
    "prepayment_trust:explain_prepay_purpose",
    "prepayment_trust:social_proof",
    "defect_risk:risk_reversal_exchange",
    "delivery_time:delivery_timeline",
    "cheaper_elsewhere:value_breakdown",
    "print_quality:print_quality",
    "out_of_stock:alternative_offer",
    "payday:payday_timing",
    "compare_brand:social_proof",
    "compare_brand:value_breakdown",
    "ask_partner:partner_summary",
})
_OBJHANDLE_TOKEN_RE = re.compile(r"\[OBJHANDLE:([a-z_]+:[a-z_]+)\]")


def _typed_objhandle_controls(body: str) -> str:
    """Convert known seed-only legacy markers into provider JSON guidance."""
    def replace(match):
        value = match.group(1)
        if value not in _OBJHANDLE_VALUES:
            raise ValueError(f"unknown managed objhandle value: {value}")
        return (
            f'JSON control {{"kind":"objhandle","value":"{value}"}} '
            "(ніколи не додавай його до reply_text)"
        )

    return _OBJHANDLE_TOKEN_RE.sub(replace, body)


OBJECTION_PLAYBOOKS = [
    {
        "title": "IG Objection · Дорого",
        "intent_tags": "objection_price",
        "priority": 80,
        "body": (
            "Спочатку погодься з відчуттям клієнта й уточни, чи ціна — єдине, що "
            "зупиняє. Пояснюй лише ціну та параметри вибраного товару з каталогу; "
            "не виводь із цього загальну якість або перевагу бренду. Після цього додай "
            "[OBJHANDLE:price:value_breakdown]."
        ),
    },
    {
        "title": "IG Objection · Подумаю",
        "intent_tags": "objection_thinking",
        "priority": 81,
        "body": (
            "Не тисни. М'яко уточни, над чим саме клієнт думає: ціна, розмір чи принт. "
            "Після такого уточнення додай [OBJHANDLE:thinking:soft_isolate]."
        ),
    },
    {
        "title": "IG Objection · Ризик розміру",
        "intent_tags": "objection_size_risk",
        "priority": 82,
        "body": (
            "Запитай зріст і заміри, допоможи звірити посадку. Не гарантуй розмір або "
            "обмін; для сервісного запиту застосовуй лише це правило: " + _SERVICE_POLICY + " "
            "Для консультації додай [OBJHANDLE:size_risk:size_consult], для сервісу — "
            "[OBJHANDLE:size_risk:risk_reversal_exchange]."
        ),
    },
    {
        "title": "IG Objection · Недовіра передоплаті",
        "intent_tags": "objection_prepayment_trust",
        "priority": 83,
        "body": (
            "Спокійно поясни призначення погодженої передоплати та не вигадуй суму. "
            "Після пояснення додай [OBJHANDLE:prepayment_trust:explain_prepay_purpose]. "
            "Якщо даєш перевірювані факти про сайт або відгуки — "
            "[OBJHANDLE:prepayment_trust:social_proof]."
        ),
    },
    {
        "title": "IG Objection · Ризик браку",
        "intent_tags": "objection_defect_risk",
        "priority": 84,
        "body": (
            "Відповідай лише перевіреним правилом обміну/повернення без абсолютних "
            "обіцянок якості: " + _SERVICE_POLICY + " Після цього додай "
            "[OBJHANDLE:defect_risk:risk_reversal_exchange]."
        ),
    },
    {
        "title": "IG Objection · Термін доставки",
        "intent_tags": "objection_delivery_time",
        "priority": 85,
        "body": (
            "Поясни строк лише цим перевіреним правилом: " + _DISPATCH_POLICY + " "
            "Конкретний дедлайн або строк кастома не вигадуй. Додай "
            "[OBJHANDLE:delivery_time:delivery_timeline]."
        ),
    },
    {
        "title": "IG Objection · Є дешевше",
        "intent_tags": "objection_cheaper_elsewhere",
        "priority": 86,
        "body": (
            "Не сперечайся з ціною конкурента. Порівняй лише підтверджені параметри "
            "вибраного товару з каталогу; не узагальнюй склад, щільність, друк чи виробництво. "
            "Додай [OBJHANDLE:cheaper_elsewhere:value_breakdown]."
        ),
    },
    {
        "title": "IG Objection · Якість принта",
        "intent_tags": "objection_print_quality",
        "priority": 87,
        "body": (
            "Для друку й догляду використовуй лише ярлик або підтверджену картку вибраного "
            "товару; не обіцяй строк служби чи невразливість. Додай "
            "[OBJHANDLE:print_quality:print_quality]."
        ),
    },
    {
        "title": "IG Objection · Немає варіанта",
        "intent_tags": "objection_out_of_stock",
        "priority": 88,
        "body": (
            "Не вигадуй наявність. Запропонуй лише фактично доступну схожу модель, колір "
            "або чергу поповнення. Після конкретної альтернативи додай "
            "[OBJHANDLE:out_of_stock:alternative_offer]."
        ),
    },
    {
        "title": "IG Objection · Після зарплати",
        "intent_tags": "objection_payday",
        "priority": 89,
        "body": (
            "Не тисни і не створюй штучний дефіцит. Узгодь конкретну доречну дату "
            "нагадування. Додай [OBJHANDLE:payday:payday_timing]."
        ),
    },
    {
        "title": "IG Objection · Порівняння брендів",
        "intent_tags": "objection_compare_brand",
        "priority": 90,
        "body": (
            "Не знецінюй інший бренд. Дай лише перевірювані відомості про вибраний товар "
            "або каталог, без загальних заяв про виробництво, тканину чи друк. Для доказів додай "
            "[OBJHANDLE:compare_brand:social_proof], для розкладу цінності — "
            "[OBJHANDLE:compare_brand:value_breakdown]."
        ),
    },
    {
        "title": "IG Objection · Порадитися",
        "intent_tags": "objection_ask_partner",
        "priority": 91,
        "body": (
            "Не тисни на рішення. Запропонуй короткий перевірюваний підсумок товару, "
            "варіанта і ціни, який зручно показати близькій людині. Додай "
            "[OBJHANDLE:ask_partner:partner_summary]."
        ),
    },
]


PLAYBOOKS = [
    {
        "title": "IG Core Sales",
        "intent_tags": "global,core,sales",
        "priority": 10,
        "body": (
            "Веди клієнта до наступного кроку без тиску: зрозумій товар, розмір, колір, "
            "кількість, для себе чи на подарунок. Пиши коротко мовою клієнта, зокрема "
            "англійською, якщо нею звернулися. Не вигадуй ціни/наявність/посилання; якщо "
            "не впевнений у товарі, попроси фото поста або уточнення. Для оплати надсилай "
            "лише персональну сторінку оформлення TwoComms, ніколи не прямий платіжний URL. "
            "На пряме питання про твою роль чесно відповідай: ти віртуальний помічник TwoComms "
            "у Direct."
        ),
    },
    {
        "title": "Product / SKU Context",
        "intent_tags": "product,catalog,product_matched,checkout",
        "priority": 20,
        "body": (
            "Якщо товар визначений, тримай саме його як основний SKU діалогу. Уточнюй тільки "
            "потрібні для пропозиції речі: фасон, розмір, колір, кількість і тип оплати. Для "
            "футболки з classic/oversize, якщо фасон не задано, постав одне запитання про фасон. "
            "Потім надішли в Direct зображення сітки саме вибраного товару й фасону, а вже "
            "потім запитуй розмір. Не збирай адресу, доставку чи email у Direct: клієнт вводить "
            "потрібні дані на персональній сторінці оформлення до створення рахунку."
        ),
    },
    {
        "title": "Size And Fit",
        "intent_tags": "size,fit",
        "priority": 30,
        "body": (
            "На питання про розмір пояснюй різницю classic/oversize. Якщо фасон не визначено, "
            "постав одне запитання про нього; якщо визначено — надішли в Direct зображення "
            "сітки саме цього товару й фасону та за потреби допоможи по замірах. Не гарантуй "
            "посадку без даних сітки та не обирай фасон чи розмір замість клієнта."
        ),
    },
    {
        "title": "Prepayment Objection",
        "intent_tags": "prepayment,payment",
        "priority": 40,
        "body": (
            REVIEWED_BODIES["Prepayment Objection"]
        ),
    },
    {
        "title": "Price Objection / Rescue",
        "intent_tags": "price,discount",
        "priority": 50,
        "body": (
            "Спочатку уточни ціновий бар’єр і поясни підтверджену ціну вибраного товару. "
            "Не обіцяй автоматичну або персональну знижку: застосовуй лише знижку, "
            "яка вже підтверджена в поточному checkout або менеджером."
        ),
    },
    {
        "title": "Custom Print Handoff",
        "intent_tags": "custom_print",
        "priority": 60,
        "body": (
            "Для кастомного принта не називай фінальну ціну й не обіцяй технічну придатність. "
            "Збери базове ТЗ; файл, права, основа, параметри й вартість підтверджує команда. "
            "Передай погоджений бриф менеджеру."
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
] + OBJECTION_PLAYBOOKS

B02_8_MANAGED_TITLES = (
    "IG Objection · Дорого",
    "IG Objection · Ризик розміру",
    "IG Objection · Ризик браку",
    "IG Objection · Термін доставки",
    "IG Objection · Є дешевше",
    "IG Objection · Якість принта",
    "IG Objection · Порівняння брендів",
    "Price Objection / Rescue",
    "Custom Print Handoff",
)

# The previous strings are exact, finite seed bodies.  Retaining them permits
# only their one-way upgrade; it never treats an administrator's variation as
# seed-owned content.
_PRE_TYPED_PLAYBOOK_BODIES = {
    item["title"]: item["body"]
    for item in PLAYBOOKS
    if "[OBJHANDLE:" in item["body"]
}
for _item in PLAYBOOKS:
    _item["body"] = _typed_objhandle_controls(_item["body"])

# A matching body means the row was created by an older known seed and may be
# upgraded. Any other body is an administrator's instruction and is untouched.
LEGACY_PLAYBOOK_BODIES = {
    "IG Core Sales": frozenset({
        "Веди клієнта до наступного кроку без тиску: зрозумій товар, розмір, колір, "
        "кількість, для себе чи на подарунок. Пиши коротко, мовою клієнта. Не вигадуй "
        "ціни/наявність/посилання; якщо не впевнений у товарі, попроси посилання на пост "
        "або уточнення.",
        "Веди клієнта до наступного кроку без тиску: зрозумій товар, розмір, колір, "
        "кількість, для себе чи на подарунок. Пиши коротко, мовою клієнта. Не вигадуй "
        "ціни/наявність/посилання; якщо не впевнений у товарі, попроси фото поста або "
        "уточнення. Для оплати надсилай лише персональну пропозицію TwoComms, ніколи не "
        "прямий платіжний URL.",
    }),
    "Product / SKU Context": frozenset({
        "Якщо товар визначений, тримай саме його як основний SKU діалогу. Уточнюй тільки "
        "потрібні для оформлення речі: розмір, колір, кількість, тип оплати і доставку.",
        "Якщо товар визначений, тримай саме його як основний SKU діалогу. Уточнюй тільки "
        "потрібні для пропозиції речі: фасон, розмір, колір, кількість і тип оплати. Для "
        "футболки з classic/oversize спершу запитай фасон, покажи сітку саме цього фасону, "
        "а потім запитуй розмір. Не збирай доставку та email у Direct: клієнт вводить їх "
        "на персональній сторінці пропозиції.",
    }),
    "Size And Fit": frozenset({
        "На питання про розмір пояснюй різницю regular/oversize і пропонуй розмірну сітку "
        "або допомогу по замірах. Не гарантуй посадку без даних з розмірної сітки.",
        "На питання про розмір пояснюй різницю classic/oversize і пропонуй відповідну "
        "розмірну сітку або допомогу по замірах. Не гарантуй посадку без даних з "
        "розмірної сітки та не обирай фасон чи розмір замість клієнта.",
    }),
    "Prepayment Objection": frozenset({
        'Передоплата — індивідуально погоджена сума, яку менеджер або клієнт явно погодили в поточній переписці для цього замовлення. Не називай 200 грн і не перенось суму з іншого клієнта чи попереднього замовлення. Пояснюй спокійно: погоджена частина вноситься зараз, залишок — за узгодженим сценарієм.',
        "Передоплата може бути лише на точну суму, яку менеджер або клієнт явно погодили "
        "в поточній переписці. Не називай фіксовані 200 грн і не перенось суму з іншого "
        "клієнта чи попереднього замовлення. Пояснюй спокійно: погоджена частина "
        "вноситься зараз, залишок — за узгодженим сценарієм.",
        "Передоплата — фіксована сума, яку менеджер або клієнт явно погодили в поточній "
        "переписці для цього замовлення. Не називай 200 грн і не перенось суму з іншого "
        "клієнта чи попереднього замовлення. Пояснюй спокійно: погоджена частина вноситься "
        "зараз, залишок — за узгодженим сценарієм.",
    }),
    "IG Objection · Термін доставки": frozenset({
        "Назви тільки перевірений строк Нової пошти 1–3 дні та окремо уточни строк "
        "виготовлення, якщо він залежить від товару. Додай "
        "[OBJHANDLE:delivery_time:delivery_timeline].",
        "Поясни окремо: зазвичай відправляємо протягом 1–3 днів, а строк у дорозі "
        "Новою Поштою залежить від маршруту та поточної роботи перевізника. Згадай, "
        "що через обстріли логістичної інфраструктури можливі затримки поза нашим "
        "контролем, і окремо уточни строк виготовлення, якщо він залежить від товару. Додай "
        "[OBJHANDLE:delivery_time:delivery_timeline].",
    }),
    "IG Objection · Дорого": frozenset({
        "Спочатку погодься з відчуттям клієнта, потім поясни цінність без знижки: "
        "щільна тканина, якісний DTF-друк і власне виробництво. Уточни, чи ціна — "
        "єдине, що зупиняє. Після фактичного пояснення додай "
        "[OBJHANDLE:price:value_breakdown].",
    }),
    "IG Objection · Ризик розміру": frozenset({
        "Запитай зріст і заміри, допоможи звірити посадку. Гарантію обміну протягом "
        "14 днів згадуй лише як відповідь на реальний страх помилки. Для консультації "
        "додай [OBJHANDLE:size_risk:size_consult], для гарантії обміну — "
        "[OBJHANDLE:size_risk:risk_reversal_exchange].",
    }),
    "IG Objection · Ризик браку": frozenset({
        "Відповідай лише перевіреними правилами обміну/повернення, без абсолютних "
        "обіцянок якості. Після конкретного правила 14 днів додай "
        "[OBJHANDLE:defect_risk:risk_reversal_exchange].",
    }),
    "IG Objection · Є дешевше": frozenset({
        "Не сперечайся з ціною конкурента. Порівняй склад, щільність, друк і виробництво "
        "лише за відомими фактами. Додай [OBJHANDLE:cheaper_elsewhere:value_breakdown].",
    }),
    "IG Objection · Якість принта": frozenset({
        "Поясни відомі властивості DTF-друку та догляд після прання, не обіцяй "
        "невразливість. Додай [OBJHANDLE:print_quality:print_quality].",
    }),
    "IG Objection · Порівняння брендів": frozenset({
        "Не знецінюй інший бренд. Дай перевірювані відмінності TwoComms: сайт, "
        "виробництво, тканина, друк. Для доказів додай "
        "[OBJHANDLE:compare_brand:social_proof], для розкладу цінності — "
        "[OBJHANDLE:compare_brand:value_breakdown].",
    }),
    "Price Objection / Rescue": frozenset({
        "Спочатку відпрацьовуй цінність: власне виробництво, якісна тканина, DTF-друк, "
        "відгуки. Не пропонуй знижку самостійно: автоматична система окремо дасть 5%, "
        "а 10% тільки як фінальний або явно узгоджений варіант."
    }),
    "Custom Print Handoff": frozenset({
        "Для кастомного принта не називай фінальну ціну. Коротко поясни: можемо зробити "
        "майже будь-який DTF-принт, ціна залежить від крою, розміру принта і готовності "
        "файлу. Збери базове ТЗ і переведи в Telegram менеджера з шапки профілю."
    }),
}
for _title, _body in _PRE_TYPED_PLAYBOOK_BODIES.items():
    LEGACY_PLAYBOOK_BODIES[_title] = frozenset(
        (*LEGACY_PLAYBOOK_BODIES.get(_title, ()), _body)
    )


class Command(BaseCommand):
    help = "Seed routed sales playbooks for the Instagram Direct bot."

    def add_arguments(self, parser):
        parser.add_argument(
            "--publish",
            action="store_true",
            help="Publish the complete post-seed instruction draft with CAS.",
        )

    def handle(self, *args, **options):
        created = 0
        updated = 0
        preserved = 0
        publication = None
        with transaction.atomic():
            from management.models import InstagramBotSettings
            from management.services.ig_policy_publication import (
                publish_instruction_policy,
                snapshot_from_rows,
                snapshot_hash,
            )

            # Every editor/publication path locks settings first.  The seed
            # follows that order, then locks all rows that make up the whole
            # draft, so a UI publication cannot observe a partial seed.
            settings_seed = InstagramBotSettings.load()
            settings_obj = InstagramBotSettings.objects.select_for_update().select_related(
                "active_instruction_publication"
            ).get(pk=settings_seed.pk)
            rows = list(BotInstruction.objects.select_for_update().order_by("priority", "id"))
            pre_snapshot = snapshot_from_rows(rows)
            pre_hash = snapshot_hash(pre_snapshot)
            pre_revision = int(settings_obj.instruction_draft_revision or 0)
            head = settings_obj.active_instruction_publication
            if options["publish"]:
                if head is None:
                    raise CommandError("cannot publish seed: active instruction publication is missing")
                if str(head.snapshot_hash or "") != pre_hash:
                    raise CommandError("cannot publish seed: instruction draft differs from the active publication")

            by_title = {}
            for row in rows:
                by_title.setdefault(row.title, row)
            for item in PLAYBOOKS:
                obj = by_title.get(item["title"])
                if obj is None:
                    obj = BotInstruction.objects.create(
                        title=item["title"],
                        body=item["body"],
                        intent_tags=item["intent_tags"],
                        priority=item["priority"],
                        is_active=True,
                    )
                    by_title[item["title"]] = obj
                    created += 1
                    continue

                managed_bodies = set(LEGACY_PLAYBOOK_BODIES.get(item["title"], ()))
                managed_bodies.add(item["body"])
                if obj.body not in managed_bodies:
                    preserved += 1
                    continue

                changed = []
                for field in ("body", "intent_tags", "priority", "is_active"):
                    value = item[field] if field in item else True
                    if getattr(obj, field) != value:
                        setattr(obj, field, value)
                        changed.append(field)
                if changed:
                    obj.save(update_fields=[*changed, "updated_at"])
                    updated += 1

            if created or updated:
                settings_obj.instruction_draft_revision = pre_revision + 1
                settings_obj.save(update_fields=["instruction_draft_revision", "updated_at"])

            if options["publish"]:
                post_rows = list(BotInstruction.objects.select_for_update().order_by("priority", "id"))
                post_snapshot = snapshot_from_rows(post_rows)
                publication = publish_instruction_policy(
                    expected_draft_revision=int(settings_obj.instruction_draft_revision),
                    expected_draft_hash=snapshot_hash(post_snapshot),
                    expected_head_id=head.pk,
                    expected_head_hash=str(head.snapshot_hash or ""),
                    note="seed_ig_bot_sales_playbooks",
                )
        self.stdout.write(self.style.SUCCESS(
            "IG sales playbooks seeded: "
            f"created={created} updated={updated} preserved={preserved} "
            f"published={bool(publication and publication.changed)} "
            f"publication_version={getattr(getattr(publication, 'publication', None), 'version', '')}."
        ))
