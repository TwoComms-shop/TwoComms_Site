"""SEO molecular-upgrade US-9 — auto-seed colour×category landings.

Replaces the original hard-coded fixture with a DB-driven loop:

1. Query every ``(Category, Color)`` pair that has at least
   ``--min-products`` published products attached via
   ``ProductColorVariant``. Anything below the threshold is skipped
   so we never publish a thin-content landing.
2. For each surviving pair, render the editorial copy + 4 FAQ items
   from a per-colour template bank. Templates carry the brand frame
   (Kharkiv, veteran-founded, DTF, Nova Poshta) so every page reads
   like a coherent piece of TwoComms copy without duplicating the
   neighbouring landing.
3. Idempotent upsert into ``CategoryColorLanding`` with
   ``is_published=True``. Re-runs update copy in place.

Usage::

    python manage.py seed_color_landings              # dry-run
    python manage.py seed_color_landings --apply
    python manage.py seed_color_landings --apply --min-products 3
    python manage.py seed_color_landings --apply --unpublish-missing
"""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Tuple

from django.core.management.base import BaseCommand
from django.db import transaction


# Per-colour template bank. The Ukrainian-language editorial paragraphs
# and FAQ items are filled in via ``str.format`` with the runtime
# tokens (category_phrase, category_url, color_label, ...). Keep keys
# in lowercase canonical form (``black``, ``coyote``, ``menthol``,
# ``pink``, ``white-burgundy``) — they map to ``english_slug_for_color_name``.
_COLOR_BANK: Dict[str, Dict[str, object]] = {
    "black": {
        "lead": (
            "Чорний — головний колір каталогу TwoComms. На ньому ми будуємо "
            "усі основні авторські серії: 225-й ОШП, харківські відсилки, "
            "«Reality Bends» з капсули Future 2026 і патріотичні принти. "
            "Чорна база добре приймає DTF-друк, не маркіє у щоденній носці "
            "і поєднується з усім іншим у гардеробі без додаткових зусиль."
        ),
        "production": (
            "Тканина пройшла попередню декатировку, фарбування рівномірне, "
            "тон стабільний партія-в-партію. Виробництво у Харкові, "
            "контроль якості у одному цеху — від крою до пакування."
        ),
        "wear": (
            "Чорний колір працює і у соло-сетапі, і в шарі: "
            "під технічну куртку, бомбер, парку. Поєднується з прямими "
            "денімами, технічними штанами, карго, робочими черевиками."
        ),
        "faq_color_q": "Які саме чорні моделі є у каталозі TwoComms?",
        "faq_color_a": (
            "У чорній серії — авторські принти 225-го ОШП, Harkiv-Edition, "
            "Pokrovsk Girl, Kha Style, Glory of Ukraine, «Reality Bends» "
            "Future 2026. Список оновлюється: дивись поточну добірку у фільтрі."
        ),
        "faq_care_q": "Чи злазить DTF-друк після прання?",
        "faq_care_a": (
            "При дотриманні режиму прання (30 °C, без хлору, виворіт) "
            "DTF-принт TwoComms тримає колір 30+ циклів без помітної "
            "деградації. Сушити горизонтально або на плічках."
        ),
    },
    "coyote": {
        "lead": (
            "Кайот — теплий пісочний відтінок, який працює як нейтральна "
            "база у міському casual і добре поєднується з мілітарі-adjacent "
            "сетапом. У TwoComms кайот заходить у серії, де класичний "
            "чорний зайвий: світліший фон не «ховає» графіку, а підкреслює."
        ),
        "production": (
            "Тканина — щільна бавовна, рівне фарбування у ванні, без "
            "плям, без різких стиків. Виробництво у Харкові; кожна партія "
            "проходить контроль кольору перед DTF-друком."
        ),
        "wear": (
            "Кайот добре поєднується з оливою, чорним низом, прямими "
            "денімами і технічними штанами. Працює і у street-сетапі, "
            "і в outdoor."
        ),
        "faq_color_q": "Чим Кайот відрізняється від хакі чи оливи?",
        "faq_color_a": (
            "Кайот — теплий пісочний з жовто-коричневим підтоном; хакі "
            "і олива — холодніший зелений з військовим акцентом. У нашому "
            "каталозі це різні відтінки із власною роллю у гардеробі."
        ),
        "faq_care_q": "Чи практичний одяг кольору «Кайот» у щоденному носінні?",
        "faq_care_a": (
            "Кайот темніший за беж і світліший за коричневий, тому він "
            "терпиміший до повсякденної носки, ніж біла база. При "
            "стандартному режимі прання тримає чистий вигляд тижнями."
        ),
    },
    "menthol": {
        "lead": (
            "Ментол — світлий пастельний зелений з легким голубим відтінком. "
            "У TwoComms цей колір зарезервований для свіжих весна-літо "
            "капсул і для серій, де треба підкреслити графіку без агресії "
            "контрасту, як на чорній базі."
        ),
        "production": (
            "Світлі базові тони вимагають акуратного фарбування — щоб уникнути "
            "плями і нерівних переходів. Ми друкуємо на ментолі тільки коли "
            "впевнені у партії тканини; у середньому це 1–2 цикли на сезон."
        ),
        "wear": (
            "Ментол добре працює влітку соло, восени — як base layer під "
            "темно-сіру або чорну верхню. Поєднується зі світлим денімом, "
            "білими кросами, легкими технічними штанами."
        ),
        "faq_color_q": "Чи змінюється ментоловий колір після прання?",
        "faq_color_a": (
            "При прання у режимі 30 °C з засобами без хлору і сушці без "
            "прямого сонячного потрапляння ментоловий тон тримається без "
            "відхилення у жовте. Найчастіша причина пожовтіння — агресивні "
            "відбілювачі, які ми не рекомендуємо."
        ),
        "faq_care_q": "Чи можна замовити кастомний друк на ментоловій базі?",
        "faq_care_a": (
            "Так, але повідом менеджеру про колір принта заздалегідь. На "
            "пастельній базі деякі темні принти потребують білої основи "
            "у DTF-друку, щоб уникнути просвічування."
        ),
    },
    "pink": {
        "lead": (
            "Рожевий у TwoComms — це не «дівчачий» колір, а свідомий вибір "
            "контрасту. Ми використовуємо його у серіях, де треба зламати "
            "очікування: насичена графіка на світлій базі, акуратний "
            "контраст, без банального «дівчина в рожевому»."
        ),
        "production": (
            "Тканина із стабільним фарбуванням, без перепадів тону. "
            "DTF-принт лягає чисто навіть на світлій основі — за рахунок "
            "білого підшару у друку."
        ),
        "wear": (
            "Рожевий добре поєднується з чорним низом, темно-синім денімом, "
            "сірим худі поверх. Підходить як для street-сетапа, так і для "
            "smart-casual з акуратними штанами."
        ),
        "faq_color_q": "Чи носять рожевий streetwear хлопці у TwoComms-аудиторії?",
        "faq_color_a": (
            "Так. Рожевий у нашому каталозі — uni-секс. Він носиться "
            "людьми, яким важлива графіка і характер бренду, а не сама "
            "по собі гендерна асоціація з кольором."
        ),
        "faq_care_q": "Чи буде рожевий колір вицвітати від сонця?",
        "faq_care_a": (
            "Незначне вицвітання можливе при тривалій носці на відкритому "
            "сонці. Сушити річ у тіні, прати з виворіту — і колір тримається "
            "сезонами без помітних змін."
        ),
    },
    "white-burgundy": {
        "lead": (
            "Біло-бордовий — це складний двокомпонентний колір, який рідко "
            "зустрічається у масовому streetwear. У TwoComms ми ввели його "
            "у каталог точково: для серій, де графіка вимагає теплої червоної "
            "акцентної зони і нейтральної світлої бази одночасно."
        ),
        "production": (
            "Двокольорове фарбування потребує окремого циклу контролю: ми "
            "перевіряємо стабільність переходу між зонами, рівномірність "
            "тонів і сумісність з DTF-друком."
        ),
        "wear": (
            "Біло-бордовий — окремий statement-look. Поєднується з чорним "
            "низом, прямими денімами, темно-сірими технічними штанами. "
            "Не потребує додаткових акцентів — сама база вже несе характер."
        ),
        "faq_color_q": "Як саме розташовані білий і бордо у цьому кольорі?",
        "faq_color_a": (
            "У серії бело-бордовий зони розподіляються залежно від крою: "
            "білий тримає передню панель і рукави, бордо — спинку або "
            "контрастні вставки. Для деяких моделей пропорції змінюються — "
            "уточнюй на сторінці конкретного товару."
        ),
        "faq_care_q": "Чи не линяє бордо на білий під час прання?",
        "faq_care_a": (
            "Ні. Тканина проходить тест на міграцію кольору перед запуском "
            "у виробництво. Прати при 30 °C, окремо від білих речей у "
            "перші 2–3 цикли — і ризик мінімальний."
        ),
    },
}


def _category_phrase(slug: str) -> Tuple[str, str]:
    """Return the (Ukrainian noun, capitalised noun) for category slug."""
    table = {
        "tshirts": ("футболка", "Футболка"),
        "hoodie": ("худі", "Худі"),
        "long-sleeve": ("лонгслів", "Лонгслів"),
    }
    return table.get(slug, (slug, slug.capitalize()))


def _category_phrase_plural(slug: str) -> str:
    table = {"tshirts": "футболки", "hoodie": "худі", "long-sleeve": "лонгсліви"}
    return table.get(slug, slug)


def _category_accusative(slug: str) -> str:
    table = {
        "tshirts": "футболку",
        "hoodie": "худі",
        "long-sleeve": "лонгслів",
    }
    return table.get(slug, slug)


_COLOR_META_FORMS = {
    "black": ("чорного кольору", "Чорні"),
    "coyote": ("кольору «Кайот»", ""),
    "menthol": ("ментолового кольору", "Ментолові"),
    "pink": ("рожевого кольору", "Рожеві"),
    "white-burgundy": ("біло-бордового кольору", "Біло-бордові"),
}


def _build_landing_meta(cat_slug: str, color_slug: str, color_label: str):
    """Build grammatically complete Ukrainian title, H1 and description."""
    plural = _category_phrase_plural(cat_slug)
    plural_cap = plural[:1].upper() + plural[1:]
    accusative = _category_accusative(cat_slug)
    descriptor, plural_adjective = _COLOR_META_FORMS.get(
        color_slug,
        (f"кольору «{color_label}»", ""),
    )
    subject = (
        f"{plural_adjective} {plural}"
        if plural_adjective
        else f"{plural_cap} {descriptor}"
    )
    print_phrase = "" if color_slug == "coyote" else " з принтом"
    title = f"Купити {accusative} {descriptor}{print_phrase} — TwoComms"
    if color_slug == "coyote":
        h1 = f"{subject} — TwoComms"
    else:
        h1 = f"{subject} TwoComms — стрітвеар з Харкова"
    description = (
        f"{subject} TwoComms з авторським DTF-друком. Щільна база, "
        "посадка regular і oversize, доставка по Україні."
    )
    return title, h1, description


def _build_editorial_html(
    cat_slug: str,
    color_label: str,
    color_slug: str,
    bank: Dict[str, str],
    product_count: int,
    cat_url: str,
) -> str:
    cat_phrase, cat_phrase_cap = _category_phrase(cat_slug)
    cat_phrase_plural = _category_phrase_plural(cat_slug)
    tokens = {
        "category_phrase": cat_phrase,
        "category_phrase_cap": cat_phrase_cap,
        "category_phrase_plural": cat_phrase_plural,
        "color_label": color_label,
        "color_slug": color_slug,
        "category_url": cat_url,
        "product_count": product_count,
    }

    lead = bank["lead"].format(**tokens)
    production = bank["production"].format(**tokens)
    wear = bank["wear"].format(**tokens)

    paragraphs = [
        f"<p>{lead}</p>",
        f"<p>У цій добірці каталог TwoComms має "
        f"{product_count} модел{'ь' if product_count == 1 else 'і' if 2 <= product_count <= 4 else 'ей'} "
        f"з авторськими принтами. Усі моделі — щільна бавовна або "
        f"трьохнитка з начосом для худі, посадка regular і oversize, "
        f"розмірний ряд S–XXL.</p>",
        f"<p>{production}</p>",
        f"<p>{wear}</p>",
        "<p>Доставка Новою Поштою або Укрпоштою по всій Україні за 1–2 "
        "робочі дні. Безкоштовна доставка при замовленні від 2 500 грн "
        "на основні відділення. Виготовлено в Харкові — повний цикл від "
        "крою до пакування під одним дахом.</p>",
    ]
    return "".join(paragraphs)


def _build_faq(cat_slug: str, color_label: str, bank: Dict[str, str]) -> List[Dict[str, str]]:
    cat_phrase, cat_phrase_cap = _category_phrase(cat_slug)
    tokens = {
        "category_phrase": cat_phrase,
        "category_phrase_cap": cat_phrase_cap,
        "color_label": color_label,
    }
    return [
        {
            "question": f"Який матеріал у виробах кольору «{color_label}»?",
            "answer": (
                f"Для футболок і лонгслівів — щільна бавовна 180–200 г/м²; "
                f"для худі — трьохнитка 320 г/м² з начосом всередині. "
                f"Виробництво у Харкові, попередня декатировка перед DTF-"
                f"друком."
            ),
        },
        {
            "question": bank["faq_color_q"].format(**tokens),
            "answer": bank["faq_color_a"].format(**tokens),
        },
        {
            "question": bank["faq_care_q"].format(**tokens),
            "answer": bank["faq_care_a"].format(**tokens),
        },
        {
            "question": "Скільки коштує доставка замовлення?",
            "answer": (
                "За тарифами Нової Пошти / Укрпошти. Безкоштовно при "
                "замовленні від 2 500 грн на основні відділення. Можлива "
                "оплата картою, Apple Pay, Google Pay або накладений платіж."
            ),
        },
    ]


class Command(BaseCommand):
    help = (
        "Auto-seed colour×category landings for every (category, color) "
        "pair that has at least N published products."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually create / update landings (default is dry-run).",
        )
        parser.add_argument(
            "--min-products",
            type=int,
            default=3,
            help="Skip pairs with fewer than this many products (default: 3).",
        )
        parser.add_argument(
            "--unpublish-missing",
            action="store_true",
            help="Unpublish landings whose pair drops below the threshold.",
        )

    def handle(self, *args, **opts):
        from productcolors.color_slug_map import english_slug_for_color_name
        from productcolors.models import ProductColorVariant
        from storefront.models import CategoryColorLanding

        apply_changes = bool(opts.get("apply"))
        min_products = max(1, int(opts.get("min_products") or 3))
        unpublish_missing = bool(opts.get("unpublish_missing"))

        # Aggregate (category_id, color_id) -> set(product_ids)
        agg: Dict[Tuple[int, int], set] = defaultdict(set)
        meta: Dict[Tuple[int, int], Dict[str, object]] = {}

        variants = (
            ProductColorVariant.objects
            .select_related("product", "product__category", "color")
        )
        for v in variants:
            cat = getattr(v.product, "category", None)
            color = v.color
            if cat is None or color is None or not color.name:
                continue
            key = (cat.id, color.id)
            agg[key].add(v.product_id)
            meta[key] = {
                "category": cat,
                "color": color,
                "category_slug": cat.slug,
                "color_name": color.name,
            }

        eligible: List[Tuple[Tuple[int, int], int]] = []
        below: List[Tuple[Tuple[int, int], int]] = []
        for key, pids in agg.items():
            count = len(pids)
            if count >= min_products:
                eligible.append((key, count))
            else:
                below.append((key, count))

        eligible.sort(key=lambda kv: -kv[1])

        created = updated = skipped = 0
        seeded_keys: set = set()
        for key, count in eligible:
            info = meta[key]
            cat = info["category"]
            color = info["color"]
            color_slug = english_slug_for_color_name(color.name)
            if not color_slug:
                self.stdout.write(self.style.WARNING(
                    f"[skip] color '{color.name}' has no English slug"
                ))
                skipped += 1
                continue
            bank = _COLOR_BANK.get(color_slug)
            if bank is None:
                self.stdout.write(self.style.WARNING(
                    f"[skip] no template bank for color '{color.name}' (slug={color_slug})"
                ))
                skipped += 1
                continue

            cat_phrase, cat_phrase_cap = _category_phrase(cat.slug)
            tokens = {
                "category_phrase": cat_phrase,
                "category_phrase_cap": cat_phrase_cap,
                "color_label": color.name,
                "color_slug": color_slug,
                "product_count": count,
            }
            cat_url = f"/catalog/{cat.slug}/"
            seo_title, seo_h1, seo_description = _build_landing_meta(
                cat.slug,
                color_slug,
                color.name,
            )
            editorial_html = _build_editorial_html(
                cat.slug, color.name, color_slug, bank, count, cat_url
            )
            faq_items = _build_faq(cat.slug, color.name, bank)

            existing = CategoryColorLanding.objects.filter(
                category=cat, color=color
            ).first()
            action = "update" if existing else "create"
            self.stdout.write(
                f"[{action}] {cat.slug} / {color.name} ({color_slug}) — "
                f"{count} products, editorial={len(editorial_html)} chars"
            )
            seeded_keys.add((cat.id, color.id))

            if not apply_changes:
                continue

            with transaction.atomic():
                if existing is None:
                    landing = CategoryColorLanding(
                        category=cat,
                        color=color,
                        seo_title=seo_title,
                        seo_h1=seo_h1,
                        seo_description=seo_description,
                        editorial_html=editorial_html,
                        faq_items=faq_items,
                        is_published=True,
                    )
                    landing.full_clean()
                    landing.save()
                    created += 1
                else:
                    existing.seo_title = seo_title
                    existing.seo_h1 = seo_h1
                    existing.seo_description = seo_description
                    existing.editorial_html = editorial_html
                    existing.faq_items = faq_items
                    existing.is_published = True
                    existing.full_clean()
                    existing.save()
                    updated += 1

        for key, count in below:
            info = meta[key]
            self.stdout.write(self.style.NOTICE(
                f"[below threshold] {info['category_slug']} / {info['color_name']} "
                f"({count} < {min_products}) — skipped"
            ))

        if unpublish_missing and apply_changes:
            for landing in CategoryColorLanding.objects.filter(is_published=True):
                if (landing.category_id, landing.color_id) not in seeded_keys:
                    landing.is_published = False
                    landing.save(update_fields=["is_published"])
                    self.stdout.write(self.style.WARNING(
                        f"[unpublish] {landing.category.slug} / {landing.color.name}"
                    ))

        if apply_changes:
            self.stdout.write(self.style.SUCCESS(
                f"Done. created={created} updated={updated} skipped={skipped}"
            ))
        else:
            self.stdout.write(self.style.NOTICE(
                "Dry-run complete. Re-run with --apply to write changes."
            ))
