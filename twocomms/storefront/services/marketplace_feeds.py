from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
import re
import xml.etree.ElementTree as ET
from urllib.parse import urljoin

from django.conf import settings
from django.utils import timezone
from django.utils.encoding import iri_to_uri
from django.utils.html import strip_tags
from django.utils.text import slugify

from storefront.models import Category, Product
from storefront.services.catalog_helpers import apply_public_product_order
from storefront.services.size_guides import resolve_product_sizes
from storefront.utils.analytics_helpers import FEED_DEFAULT_COLOR, get_item_group_id, get_offer_id


GOOGLE_NS = "http://base.google.com/ns/1.0"
G = f"{{{GOOGLE_NS}}}"
ET.register_namespace("g", GOOGLE_NS)

CDATA_START = "___TWC_CDATA_START___"
CDATA_END = "___TWC_CDATA_END___"

SHOP_NAME = "TwoComms"
SHOP_COMPANY = "TWOCOMMS"
BUYME_SHOP_NAME = "Брендовий одяг"
DEFAULT_CURRENCY = "UAH"
DEFAULT_GOOGLE_PRODUCT_CATEGORY = "1604"  # Apparel & Accessories > Clothing
PLACEHOLDER_IMAGE_PATH = "/static/img/placeholder.jpg"

DEFAULT_ROZETKA_CATEGORY_RZ_ID_MAP = {
    # Public Rozetka category URLs:
    # /mugskie-futbolki/c4637839/, /mugskie-longslivi/c4637855/, /mugskie-hudi/c4637959/
    "futbolki": "4637839",
    "tshirt": "4637839",
    "tshirts": "4637839",
    "t-shirt": "4637839",
    "t-shirts": "4637839",
    "footballki": "4637839",
    "hoodie": "4637959",
    "hoodies": "4637959",
    "hudi": "4637959",
    "khudi": "4637959",
    "long-sleeve": "4637855",
    "longsleeve": "4637855",
    "longsliv": "4637855",
    "longslivy": "4637855",
}

HEX_COLOR_LABELS_UA = {
    "000000": "Чорний",
    "111111": "Чорний",
    "FFFFFF": "Білий",
    "F5F5F5": "Білий",
    "808080": "Сірий",
    "FF0000": "Червоний",
    "0000FF": "Синій",
    "008000": "Зелений",
    "FFFF00": "Жовтий",
    "FFA500": "Помаранчевий",
    "800080": "Фіолетовий",
    "A52A2A": "Коричневий",
    "59604A": "Олива",
    "5C6449": "Олива",
    "7A5A3A": "Койот",
    "8B6B45": "Койот",
}

COLOR_RU = {
    "Чорний": "Черный",
    "Білий": "Белый",
    "Сірий": "Серый",
    "Червоний": "Красный",
    "Синій": "Синий",
    "Зелений": "Зеленый",
    "Жовтий": "Желтый",
    "Помаранчевий": "Оранжевый",
    "Фіолетовий": "Фиолетовый",
    "Коричневий": "Коричневый",
    "Олива": "Олива",
    "Койот": "Койот",
    "Хакі": "Хаки",
}

UK_TO_RU_TEXT_REPLACEMENTS = (
    ("Довіряй Своїй Божевільній Ідеї", "Доверяй Своей Безумной Идее"),
    ("Довіряй своїй божевільній ідеї", "Доверяй своей безумной идее"),
    ("Колекція", "Коллекция"),
    ("Печатка Хулігана", "Печать Хулигана"),
    ("Дрони навколо", "Дроны вокруг"),
    ("Команда Сірко", "Команда Сирко"),
    ("Життя Бентежне", "Жизнь Тревожная"),
    ("мені поЖуй — це філософія", "мне поЖуй — это философия"),
    ("Харківська Область", "Харьковская Область"),
    ("Харків / Стиль — Це Все", "Харьков / Стиль — Это Все"),
    ("Харків - Стиль Це Все", "Харьков - Стиль Это Все"),
    ("Харків Edition", "Харьков Edition"),
    ("І На Той Світ З Собою Візьму", "И На Тот Свет С Собой Возьму"),
    ("Річ Йде Про Двозначні Суми", "Речь Идет О Двузначных Суммах"),
    ("Дівчина З Покровська", "Девушка Из Покровска"),
    ("Бізнес — Це Математика", "Бизнес — Это Математика"),
    ("Тильний В Єбало", "Тыльный В Ебало"),
    ("Тильний В Єб*Ло", "Тыльный В Еб*Ло"),
    ("Де Мої Подарунки, Мразота?", "Где Мои Подарки, Мразота?"),
    ("ЛОНГСЛІВ", "ЛОНГСЛИВ"),
    ("Лонгсліви", "Лонгсливы"),
    ("лонгсліви", "лонгсливы"),
    ("Лонгслів", "Лонгслив"),
    ("лонгслів", "лонгслив"),
    ("ХУДІ", "ХУДИ"),
    ("Худі", "Худи"),
    ("худі", "худи"),
    ("Чорна", "Черная"),
    ("чорна", "черная"),
    ("Чорний", "Черный"),
    ("чорний", "черный"),
    ("Білий", "Белый"),
    ("білий", "белый"),
    ("Сірий", "Серый"),
    ("сірий", "серый"),
    ("Жіночий", "Женский"),
    ("жіночий", "женский"),
    ("Чоловічий", "Мужской"),
    ("чоловічий", "мужской"),
    ("Класичний", "Классический"),
    ("класичний", "классический"),
    ("класичне", "классическое"),
    ("класична", "классическая"),
    ("Унісекс", "Унисекс"),
    ("унісекс", "унисекс"),
    ("Світ", "Свет"),
    ("світ", "свет"),
    ("Своїй", "Своей"),
    ("своїй", "своей"),
    ("Божевільній", "Безумной"),
    ("божевільній", "безумной"),
    ("Ідеї", "Идее"),
    ("ідеї", "идее"),
    ("Харків", "Харьков"),
    ("Покровськ", "Покровск"),
    ("Покровська", "Покровска"),
    ("Одяг", "Одежда"),
    ("одяг", "одежда"),
    ("Мої", "Мои"),
    ("мої", "мои"),
)

BUYME_FORBIDDEN_BRAND_TERMS = (
    "TwoComms",
    "Two Comms",
    "TWOCOMMS",
    "TWC",
    "Nike",
    "Adidas",
    "Puma",
    "Reebok",
    "New Balance",
    "The North Face",
    "Stone Island",
    "Balenciaga",
    "Gucci",
    "Prada",
    "Louis Vuitton",
    "Supreme",
    "Off-White",
    "Jordan",
    "Apple",
    "Samsung",
    "Sony",
    "iPhone",
    "AirPods",
)
BUYME_BRAND_RE = re.compile(
    r"(?iu)(?<![\w])(?:"
    + "|".join(re.escape(term) for term in sorted(BUYME_FORBIDDEN_BRAND_TERMS, key=len, reverse=True))
    + r")(?![\w])"
)
BUYME_TITLE_NOISE_RE = re.compile(
    r"(?iu)(?<![\w])(?:"
    r"чорна|чорний|біла|білий|сірий|сіра|футболка|футболки|лонгслів|лонгсліви|худі|hoodie|t-?shirt|"
    r"унісекс|unisex|жіночий|чоловічий|жіноча|чоловіча"
    r")(?![\w])"
)
BUYME_KIND_LABELS = {
    "tshirt": "Брендова футболка",
    "longsleeve": "Брендовий лонгслів",
    "hoodie": "Брендове худі",
    "apparel": "Брендовий одяг",
}
BUYME_DROP_DISCOUNT_RATES = {
    "tshirt": Decimal("0.30"),
    "longsleeve": Decimal("0.10"),
    "hoodie": Decimal("0.22"),
    "apparel": Decimal("0.20"),
}
BUYME_MIN_QUANTITY = 100
BEZZET_MIN_QUANTITY = 100
# Floor для всіх marketplace-фідів: TwoComms працює як DTF / made-to-order,
# тому залишок завжди має бути «у наявності». Інакше Rozetka / Kasta / Prom
# / Bezzet тощо інтерпретують stock_quantity=0 як «немає в наявності»
# незалежно від атрибута available="true".
FEED_MIN_QUANTITY = 100

CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
PRICE_TEXT_RE = re.compile(r"(?i)(ціна|цена|price)\s*[:\-]?[^\n<]*|\d+[\s.,]*(?:грн|uah|₴)")
URL_TEXT_RE = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
CDATA_RE = re.compile(
    re.escape(CDATA_START).encode("utf-8") + b"(.*?)" + re.escape(CDATA_END).encode("utf-8"),
    re.DOTALL,
)


@dataclass(frozen=True)
class FeedOffer:
    product: Product
    variant_id: int | None
    sku: str
    barcode: str
    stock: int
    size: str
    color_ua: str
    color_ru: str
    image_urls: list[str]
    google_offer_id: str
    yml_offer_id: str
    rozetka_offer_id: str
    article: str
    group_id: str
    product_url: str
    base_price: int
    price: int
    old_price: int | None
    material_ua: str
    material_ru: str
    description_ua: str
    description_ru: str
    google_description: str
    video_link: str

    @property
    def available(self) -> bool:
        # Всі товари у фідах позначаємо як «в наявності» незалежно від поточних залишків.
        return True


def resolve_base_url(base_url: str | None = None) -> str:
    configured = (
        base_url
        or getattr(settings, "FEED_BASE_URL", "")
        or getattr(settings, "SITE_BASE_URL", "")
        or "https://twocomms.shop"
    )
    return str(configured).rstrip("/")


def _clean_xml_text(value: object) -> str:
    text = "" if value is None else str(value)
    text = CONTROL_CHARS_RE.sub("", text)
    return text.strip()


def _collapse_plain_text(value: object) -> str:
    text = strip_tags(_clean_xml_text(value))
    text = PRICE_TEXT_RE.sub("", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _truncate(value: str, limit: int) -> str:
    value = _clean_xml_text(value)
    if len(value) <= limit:
        return value
    return value[: max(limit - 1, 0)].rstrip() + "…"


def _format_google_price(value: int | Decimal) -> str:
    amount = Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{amount} {DEFAULT_CURRENCY}"


def _format_yml_price(value: int | Decimal) -> str:
    amount = Decimal(str(value or 0)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return str(int(amount))


def _format_buyme_price(value: int | Decimal) -> str:
    amount = Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{amount:.2f}"


def _absolute_url(base_url: str, path: str | None) -> str:
    if not path:
        return ""
    raw = str(path)
    if raw.startswith(("http://", "https://")):
        return iri_to_uri(raw)
    if not raw.startswith("/"):
        raw = f"/{raw}"
    return iri_to_uri(urljoin(base_url, raw))


def _field_url(field) -> str:
    if not field:
        return ""
    try:
        return field.url or ""
    except Exception:
        return ""


def _dedupe(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value and value not in seen:
            result.append(value)
            seen.add(value)
    return result


def _color_from_variant(variant) -> str:
    color = getattr(variant, "color", None)
    name = _clean_xml_text(getattr(color, "name", ""))
    if name:
        return name[0].upper() + name[1:]

    primary = _clean_xml_text(getattr(color, "primary_hex", "")).lstrip("#").upper()
    secondary = _clean_xml_text(getattr(color, "secondary_hex", "")).lstrip("#").upper()
    primary_name = HEX_COLOR_LABELS_UA.get(primary)
    secondary_name = HEX_COLOR_LABELS_UA.get(secondary)
    if primary_name and secondary_name and primary_name != secondary_name:
        return f"{primary_name}/{secondary_name}"
    return primary_name or secondary_name or FEED_DEFAULT_COLOR


def _color_ru(color_ua: str) -> str:
    parts = [part.strip() for part in (color_ua or "").split("/") if part.strip()]
    if len(parts) > 1:
        return "/".join(COLOR_RU.get(part, part) for part in parts)
    return COLOR_RU.get(color_ua, color_ua or "Черный")


def _ascii_token(value: object, fallback: str) -> str:
    slug = slugify(str(value or ""), allow_unicode=False).replace("-", "").upper()
    return slug or fallback


def _rozetka_offer_id(product_id: int, variant_id: int | None, size: str) -> str:
    variant_part = f"CV{variant_id}" if variant_id else "D"
    size_part = _ascii_token(size, "SIZE")
    return f"TC{int(product_id):04d}{variant_part}{size_part}"


def _article_for_offer(product_id: int, variant_id: int | None, sku: str, color_ua: str) -> str:
    if sku:
        return _truncate(sku, 64)
    if variant_id:
        return f"TC{int(product_id):04d}CV{variant_id}"
    color_part = _ascii_token(color_ua, "COLOR")
    return f"TC{int(product_id):04d}{color_part}"


def _uk_text_to_ru(value: object) -> str:
    result = _clean_xml_text(value)
    for source, replacement in UK_TO_RU_TEXT_REPLACEMENTS:
        result = result.replace(source, replacement)
    return result


def _contains_buyme_forbidden_brand(value: object) -> bool:
    return bool(BUYME_BRAND_RE.search(_clean_xml_text(value)))


def _sanitize_buyme_text(value: object) -> str:
    text = _clean_xml_text(value)
    text = URL_TEXT_RE.sub("", text)
    text = PRICE_TEXT_RE.sub("", text)
    text = BUYME_BRAND_RE.sub("", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"([«])\s+", r"\1", text)
    text = re.sub(r"\s+([»])", r"\1", text)
    text = re.sub(r"\s{2,}", " ", text)
    text = re.sub(r"\s*[-–—/]\s*(?=$)", "", text)
    text = re.sub(r"^(?:[-–—/|,.;:]\s*)+", "", text)
    return text.strip()


def _buyme_design_phrase(product: Product) -> str:
    title = _sanitize_buyme_text(getattr(product, "title", ""))
    title = BUYME_TITLE_NOISE_RE.sub("", title)
    title = re.sub(r"\s+([,.;:!?])", r"\1", title)
    title = re.sub(r"([«])\s+", r"\1", title)
    title = re.sub(r"\s+([»])", r"\1", title)
    title = re.sub(r"\s{2,}", " ", title)
    title = re.sub(r"^(?:[-–—/|,.;:]\s*)+", "", title)
    title = re.sub(r"\s*(?:[-–—/|,.;:])+$", "", title)
    return title.strip() or "з авторським принтом"


def _buyme_base_name(product: Product) -> str:
    kind = _product_kind(product)
    label = BUYME_KIND_LABELS.get(kind, BUYME_KIND_LABELS["apparel"])
    design = _buyme_design_phrase(product)
    return _truncate(_sanitize_buyme_text(f"{label} унісекс {design}"), 255)


def _buyme_offer_id(product_id: int, variant_id: int | None, size: str, color_ua: str) -> str:
    variant_part = f"v{variant_id}" if variant_id else "default"
    size_part = _ascii_token(size, "size").lower()
    color_part = _ascii_token(color_ua, "color").lower()
    return _truncate(f"buyme-{int(product_id)}-{variant_part}-{color_part}-{size_part}", 64)


def _buyme_group_id(offer: FeedOffer) -> str:
    product_id = int(offer.product.id)
    if offer.variant_id:
        return _truncate(f"buyme-{product_id}-v{offer.variant_id}", 64)
    color_part = _ascii_token(offer.color_ua, "color").lower()
    return _truncate(f"buyme-{product_id}-default-{color_part}", 64)


def _buyme_retail_price(offer: FeedOffer) -> int:
    product = offer.product
    candidates = [
        int(offer.base_price or 0),
        int(offer.old_price or 0),
        int(getattr(product, "recommended_price", 0) or 0),
    ]
    return max(candidates)


def _buyme_drop_price(product: Product, retail_price: int) -> Decimal:
    kind = _product_kind(product)
    rate = BUYME_DROP_DISCOUNT_RATES.get(kind, BUYME_DROP_DISCOUNT_RATES["apparel"])
    drop_multiplier = Decimal("1") - rate
    return (Decimal(str(retail_price or 0)) * drop_multiplier).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _buyme_quantity(offer: FeedOffer) -> int:
    return max(int(offer.stock or 0), BUYME_MIN_QUANTITY)


def _bezzet_quantity(offer: FeedOffer) -> int:
    return max(int(offer.stock or 0), BEZZET_MIN_QUANTITY)


def _feed_stock_quantity(offer: FeedOffer) -> int:
    """Уніфікований floor для tag <stock_quantity> у фідах.

    Поверне max(offer.stock, FEED_MIN_QUANTITY). Це гарантує, що навіть
    коли всі ProductColorVariant мають stock=0 у БД (made-to-order DTF
    модель), фіди Rozetka / Kasta / Prom все одно показують товар як
    у наявності, а не sold out.
    """
    return max(int(offer.stock or 0), FEED_MIN_QUANTITY)


def _bezzet_group_id(offer: FeedOffer) -> str:
    if offer.variant_id:
        variant_part = str(offer.variant_id)
    else:
        variant_part = _ascii_token(offer.color_ua, "color").lower()
    return _truncate(f"{int(offer.product.id)}-{variant_part}", 64)


def _buyme_description(product: Product, offer: FeedOffer) -> str:
    name = _buyme_base_name(product)
    kind = _product_kind(product)
    item_label = BUYME_KIND_LABELS.get(kind, BUYME_KIND_LABELS["apparel"]).lower()
    text = (
        f"{name}. {item_label.capitalize()} з авторським принтом для щоденного образу, "
        "streetwear-стилю та продажу у форматі дропшипінгу. "
        f"Матеріал: {offer.material_ua}. Колір: {offer.color_ua}. Розмір: {offer.size}. "
        "Сезон: демісезон. Стать: унісекс. Стан: новий. Країна виробництва: Україна."
    )
    return _truncate(_sanitize_buyme_text(text), 3000)


def _kasta_article_for_product(product: Product) -> str:
    return f"TC{int(product.id):04d}"


def _kasta_name_ru(title: str) -> str:
    return _uk_text_to_ru(title)


def is_valid_gtin(gtin: str) -> bool:
    value = _clean_xml_text(gtin)
    if not value.isdigit() or len(value) not in (8, 12, 13, 14):
        return False
    digits = [int(char) for char in value]
    check_digit = digits.pop()
    digits.reverse()
    total = sum(digit * (3 if idx % 2 == 0 else 1) for idx, digit in enumerate(digits))
    return (10 - (total % 10)) % 10 == check_digit


def _product_kind(product: Product) -> str:
    source = " ".join(
        filter(
            None,
            [
                getattr(product, "title", ""),
                getattr(product, "slug", ""),
                getattr(getattr(product, "category", None), "name", ""),
                getattr(getattr(product, "category", None), "slug", ""),
                getattr(getattr(product, "catalog", None), "name", ""),
                getattr(getattr(product, "catalog", None), "slug", ""),
            ],
        )
    ).lower()
    if any(token in source for token in ("худі", "худи", "hood", "hudi", "hoodie")):
        return "hoodie"
    if any(token in source for token in ("лонг", "long", "longsleeve", "лонгслів")):
        return "longsleeve"
    if any(token in source for token in ("футболк", "tshirt", "t-shirt", "tee", "futbol")):
        return "tshirt"
    return "apparel"


def _material_pair(product: Product) -> tuple[str, str]:
    kind = _product_kind(product)
    if kind == "hoodie":
        return "90% бавовна, 10% поліестер", "90% хлопок, 10% полиэстер"
    if kind == "longsleeve":
        return "95% бамбук, 5% еластан", "95% бамбук, 5% эластан"
    return "95% бавовна, 5% еластан", "95% хлопок, 5% эластан"


def _sale_price(product: Product, base_price: int) -> int:
    discount = int(getattr(product, "discount_percent", 0) or 0)
    if discount <= 0:
        return int(base_price or 0)
    return max(
        1,
        int(
            (Decimal(base_price) * Decimal(100 - discount) / Decimal(100)).quantize(
                Decimal("1"),
                rounding=ROUND_HALF_UP,
            )
        ),
    )


def _old_price(product: Product, base_price: int, sale_price: int) -> int | None:
    recommended = int(getattr(product, "recommended_price", 0) or 0)
    if recommended > sale_price:
        return recommended
    if base_price > sale_price:
        return base_price
    return None


def _source_description(product: Product) -> str:
    """Pick the best human-authored description for marketplace feeds.

    Manual SEO fields take priority. AI description is consulted only when the
    product opted into AI generation (ai_generation_enabled=True).
    """
    candidates = ["full_description", "description", "short_description", "seo_description"]
    if getattr(product, "ai_generation_enabled", False):
        candidates.append("ai_description")
    for field_name in candidates:
        value = _collapse_plain_text(getattr(product, field_name, ""))
        if value:
            return value
    return ""


def _description_html_ru(product: Product, offer_context: dict) -> str:
    title = offer_context["title"]
    category = offer_context["category_ru"]
    color = offer_context["color_ru"]
    size = offer_context["size"]
    material = offer_context["material_ru"]
    title = _uk_text_to_ru(title)
    category = _uk_text_to_ru(category)
    lead = f"{title} от TwoComms — качественная вещь категории {category.lower()} с эксклюзивным дизайном."
    return "".join(
        [
            f"<p>{_clean_xml_text(lead)}</p>",
            (
                "<p>Эксклюзивный дизайн TwoComms подходит для повседневного образа, "
                "стритвир-стиля, подарка или мерча команды. Изделие рассчитано на "
                "комфортную посадку и регулярную носку.</p>"
            ),
            f"<p>Материал: {material}. Цвет: {color}. Размер: {size}. Сезон: демисезон. Производство: Украина.</p>",
        ]
    )


def _description_html_ua(product: Product, offer_context: dict) -> str:
    title = offer_context["title"]
    category = offer_context["category_ua"]
    color = offer_context["color_ua"]
    size = offer_context["size"]
    material = offer_context["material_ua"]
    raw = _source_description(product)
    lead = raw if raw else f"{title} від TwoComms — якісна річ категорії {category.lower()} з ексклюзивним дизайном."
    return "".join(
        [
            f"<p>{_clean_xml_text(lead)}</p>",
            (
                "<p>Ексклюзивний дизайн TwoComms підходить для щоденного образу, "
                "streetwear-стилю, подарунка або мерчу команди. Виріб розрахований "
                "на комфортну посадку та регулярне носіння.</p>"
            ),
            f"<p>Матеріал: {material}. Колір: {color}. Розмір: {size}. Сезон: демісезон. Виробництво: Україна.</p>",
        ]
    )


def _google_description(product: Product, offer_context: dict) -> str:
    html = _description_html_ua(product, offer_context)
    return _truncate(_collapse_plain_text(html), 5000)


def _kasta_description(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", _clean_xml_text(html))
    text = URL_TEXT_RE.sub("", text)
    text = PRICE_TEXT_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return _truncate(text, 5000)


def _video_link(product: Product) -> str:
    # Приоритет — отдельное поле video_url (YouTube). Преобразуем в
    # канонический watch-URL, который принимает Merchant Center.
    direct = _clean_xml_text(getattr(product, "video_url", ""))
    if direct:
        try:
            from storefront.utils.video import extract_youtube_id, youtube_watch_url
            video_id = extract_youtube_id(direct)
            if video_id:
                return iri_to_uri(youtube_watch_url(video_id))
        except Exception:
            pass
        if direct.startswith(("http://", "https://")):
            return iri_to_uri(direct)

    schema = getattr(product, "seo_schema", None)
    candidates = []
    if isinstance(schema, dict):
        for key in ("video_link", "video_url", "video"):
            value = schema.get(key)
            if isinstance(value, str):
                candidates.append(value)
        videos = schema.get("videos")
        if isinstance(videos, list):
            candidates.extend(item for item in videos if isinstance(item, str))
    for candidate in candidates:
        value = _clean_xml_text(candidate)
        if value.startswith(("http://", "https://")):
            return iri_to_uri(value)
    return ""


def _product_url(base_url: str, product: Product, *, size: str, color_slug: str = "") -> str:
    """Return the canonical PDP URL for the exact feed variant.

    Product redirects only understand a numeric ``?color=<variant_id>`` while
    the old feed passed the translated colour label.  The size redirect then
    discarded that unresolved colour.  Feed links now bypass legacy query
    handling and use the same colour/size path contract as PDP canonicals.
    """
    slug = getattr(product, "slug", "") or f"product-{product.id}"
    segments = [str(value).strip().lower() for value in (color_slug, size) if str(value).strip()]
    variant_path = "/".join(segments)
    path = f"/product/{slug}/{variant_path}/" if variant_path else f"/product/{slug}/"
    return iri_to_uri(urljoin(base_url, path))


def _collect_base_images(product: Product, base_url: str) -> list[str]:
    paths = []
    for field_name in ("main_image", "home_card_image"):
        path = _field_url(getattr(product, field_name, None))
        if path:
            paths.append(path)
    for image in product.images.all():
        path = _field_url(getattr(image, "image", None))
        if path:
            paths.append(path)
    return _dedupe([_absolute_url(base_url, path) for path in paths])


def _collect_variant_images(variant, base_url: str) -> list[str]:
    paths = []
    for image in variant.images.all():
        path = _field_url(getattr(image, "image", None))
        if path:
            paths.append(path)
    return _dedupe([_absolute_url(base_url, path) for path in paths])


def published_products_queryset():
    return apply_public_product_order(
        Product.objects.filter(status="published")
        .select_related("category", "catalog", "size_grid")
        .prefetch_related(
            "catalog__options__values",
            "catalog__size_grids",
            "images",
            "color_variants__color",
            "color_variants__images",
        )
    )


def iter_feed_offers(base_url: str | None = None, products=None) -> list[FeedOffer]:
    base_url = resolve_base_url(base_url)
    products = list(products if products is not None else published_products_queryset())
    offers: list[FeedOffer] = []

    for product in products:
        material_ua, material_ru = _material_pair(product)
        base_images = _collect_base_images(product, base_url)
        category_ua = _clean_xml_text(getattr(getattr(product, "category", None), "name", "")) or "Одяг"
        category_ru = _uk_text_to_ru(category_ua)
        group_id = get_item_group_id(product.id)
        video_link = _video_link(product)
        sizes = resolve_product_sizes(product) or ["S", "M", "L", "XL", "XXL"]
        color_variants = list(product.color_variants.all())

        if not color_variants:
            color_ua = FEED_DEFAULT_COLOR
            color_ru = _color_ru(color_ua)
            image_urls = base_images or [_absolute_url(base_url, getattr(settings, "FEED_PLACEHOLDER_IMAGE_URL", PLACEHOLDER_IMAGE_PATH))]
            stock = 100 if getattr(product, "is_dropship_available", True) else 0
            base_price = int(getattr(product, "price", 0) or 0)
            price = _sale_price(product, base_price)
            old_price = _old_price(product, base_price, price)

            for size in sizes:
                google_offer_id = get_offer_id(product.id, None, size, color_ua)
                article = _article_for_offer(product.id, None, "", color_ua)
                context = {
                    "title": product.title,
                    "category_ua": category_ua,
                    "category_ru": category_ru,
                    "color_ua": color_ua,
                    "color_ru": color_ru,
                    "size": size,
                    "material_ua": material_ua,
                    "material_ru": material_ru,
                }
                offers.append(
                    FeedOffer(
                        product=product,
                        variant_id=None,
                        sku="",
                        barcode="",
                        stock=stock,
                        size=size,
                        color_ua=color_ua,
                        color_ru=color_ru,
                        image_urls=image_urls,
                        google_offer_id=google_offer_id,
                        yml_offer_id=f"{product.id}-0-{size}",
                        rozetka_offer_id=_rozetka_offer_id(product.id, None, size),
                        article=article,
                        group_id=group_id,
                        product_url=_product_url(base_url, product, size=size),
                        base_price=base_price,
                        price=price,
                        old_price=old_price,
                        material_ua=material_ua,
                        material_ru=material_ru,
                        description_ua=_description_html_ua(product, context),
                        description_ru=_description_html_ru(product, context),
                        google_description=_google_description(product, context),
                        video_link=video_link,
                    )
                )
            continue

        for variant in color_variants:
            color_ua = _color_from_variant(variant)
            color_ru = _color_ru(color_ua)
            variant_images = _collect_variant_images(variant, base_url)
            image_urls = _dedupe(variant_images + base_images)
            if not image_urls:
                image_urls = [_absolute_url(base_url, getattr(settings, "FEED_PLACEHOLDER_IMAGE_URL", PLACEHOLDER_IMAGE_PATH))]
            stock = int(getattr(variant, "stock", 0) or 0)
            sku = _clean_xml_text(getattr(variant, "sku", ""))
            barcode = _clean_xml_text(getattr(variant, "barcode", ""))
            base_price = int(getattr(variant, "price_override", None) or getattr(product, "price", 0) or 0)
            price = _sale_price(product, base_price)
            old_price = _old_price(product, base_price, price)
            article = _article_for_offer(product.id, variant.id, sku, color_ua)

            for size in sizes:
                google_offer_id = get_offer_id(product.id, variant.id, size, color_ua)
                context = {
                    "title": product.title,
                    "category_ua": category_ua,
                    "category_ru": category_ru,
                    "color_ua": color_ua,
                    "color_ru": color_ru,
                    "size": size,
                    "material_ua": material_ua,
                    "material_ru": material_ru,
                }
                offers.append(
                    FeedOffer(
                        product=product,
                        variant_id=variant.id,
                        sku=sku,
                        barcode=barcode,
                        stock=stock,
                        size=size,
                        color_ua=color_ua,
                        color_ru=color_ru,
                        image_urls=image_urls,
                        google_offer_id=google_offer_id,
                        yml_offer_id=f"{product.id}-{variant.id}-{size}",
                        rozetka_offer_id=_rozetka_offer_id(product.id, variant.id, size),
                        article=article,
                        group_id=group_id,
                        product_url=_product_url(
                            base_url,
                            product,
                            size=size,
                            color_slug=getattr(variant, "slug", ""),
                        ),
                        base_price=base_price,
                        price=price,
                        old_price=old_price,
                        material_ua=material_ua,
                        material_ru=material_ru,
                        description_ua=_description_html_ua(product, context),
                        description_ru=_description_html_ru(product, context),
                        google_description=_google_description(product, context),
                        video_link=video_link,
                    )
                )

    return offers


def _category_rz_id(category: Category) -> str:
    configured = getattr(settings, "ROZETKA_CATEGORY_RZ_ID_MAP", {}) or {}
    mapping = {**DEFAULT_ROZETKA_CATEGORY_RZ_ID_MAP, **{str(k).lower(): str(v) for k, v in configured.items()}}
    candidates = [
        getattr(category, "slug", ""),
        slugify(getattr(category, "name", ""), allow_unicode=False),
        _clean_xml_text(getattr(category, "name", "")).lower(),
    ]
    for candidate in candidates:
        value = mapping.get(str(candidate).lower())
        if value:
            return value
    return ""


def _used_categories(offers: list[FeedOffer]) -> list[Category]:
    categories = {}
    for offer in offers:
        category = getattr(offer.product, "category", None)
        if category and category.id not in categories:
            categories[category.id] = category
    return [categories[key] for key in sorted(categories)]


def _append_cdata(parent: ET.Element, tag: str, html: str) -> ET.Element:
    element = ET.SubElement(parent, tag)
    element.text = f"{CDATA_START}{_clean_xml_text(html)}{CDATA_END}"
    return element


def _finalize_xml(root: ET.Element) -> bytes:
    ET.indent(root, space="  ", level=0)
    payload = ET.tostring(root, encoding="utf-8", xml_declaration=True)

    def replace_cdata(match: re.Match[bytes]) -> bytes:
        content = match.group(1)
        content = content.replace(b"&lt;", b"<").replace(b"&gt;", b">").replace(b"&amp;", b"&")
        content = content.replace(b"]]>", b"]]]]><![CDATA[>")
        return b"<![CDATA[" + content + b"]]>"

    return CDATA_RE.sub(replace_cdata, payload)


def build_rozetka_feed_xml(base_url: str | None = None) -> bytes:
    base_url = resolve_base_url(base_url)
    offers = iter_feed_offers(base_url)

    catalog = ET.Element("yml_catalog", {"date": timezone.now().strftime("%Y-%m-%d %H:%M")})
    shop = ET.SubElement(catalog, "shop")
    ET.SubElement(shop, "name").text = SHOP_NAME
    ET.SubElement(shop, "company").text = SHOP_COMPANY
    ET.SubElement(shop, "url").text = base_url

    currencies = ET.SubElement(shop, "currencies")
    ET.SubElement(currencies, "currency", {"id": DEFAULT_CURRENCY, "rate": "1"})

    categories_el = ET.SubElement(shop, "categories")
    for category in _used_categories(offers):
        attrs = {"id": str(category.id)}
        rz_id = _category_rz_id(category)
        if rz_id:
            attrs["rz_id"] = rz_id
        ET.SubElement(categories_el, "category", attrs).text = _truncate(category.name, 255)

    offers_el = ET.SubElement(shop, "offers")
    for offer in offers:
        product = offer.product
        offer_el = ET.SubElement(
            offers_el,
            "offer",
            {"id": offer.rozetka_offer_id, "available": "true" if offer.available else "false"},
        )
        ET.SubElement(offer_el, "url").text = _truncate(offer.product_url, 500)
        ET.SubElement(offer_el, "price").text = _format_yml_price(offer.price)
        if offer.old_price and offer.old_price > offer.price:
            ET.SubElement(offer_el, "price_old").text = _format_yml_price(offer.old_price)
        ET.SubElement(offer_el, "currencyId").text = DEFAULT_CURRENCY
        ET.SubElement(offer_el, "categoryId").text = str(product.category_id)
        for image_url in offer.image_urls[:15]:
            ET.SubElement(offer_el, "picture").text = _truncate(image_url, 1999)
        ET.SubElement(offer_el, "vendor").text = SHOP_NAME
        ET.SubElement(offer_el, "article").text = offer.article
        ET.SubElement(offer_el, "stock_quantity").text = str(_feed_stock_quantity(offer))

        name_ru = _truncate(f"{_uk_text_to_ru(product.title)} {offer.color_ru} {offer.size}", 255)
        name_ua = _truncate(f"{product.title} {offer.color_ua} {offer.size}", 255)
        ET.SubElement(offer_el, "name").text = name_ru
        ET.SubElement(offer_el, "name_ua").text = name_ua
        _append_cdata(offer_el, "description", offer.description_ru)
        _append_cdata(offer_el, "description_ua", offer.description_ua)
        ET.SubElement(offer_el, "state").text = "new"

        params = [
            ("Розмір", offer.size),
            ("Колір", offer.color_ua),
            ("Склад", offer.material_ua),
            ("Матеріал", offer.material_ua),
            ("Сезон", "Демісезон"),
            ("Стать", "Унісекс"),
            ("Вікова група", "Дорослі"),
            ("Принт", "Є"),
            ("Країна-виробник товару", "Україна"),
        ]
        for name, value in params:
            clean_value = _truncate(value, 500)
            if clean_value:
                ET.SubElement(offer_el, "param", {"name": name}).text = clean_value

    return _finalize_xml(catalog)


def build_kasta_feed_xml(base_url: str | None = None) -> bytes:
    base_url = resolve_base_url(base_url)
    offers = iter_feed_offers(base_url)

    catalog = ET.Element("yml_catalog", {"date": timezone.now().strftime("%Y-%m-%d %H:%M")})
    shop = ET.SubElement(catalog, "shop")
    ET.SubElement(shop, "name").text = SHOP_NAME
    ET.SubElement(shop, "company").text = SHOP_COMPANY
    ET.SubElement(shop, "url").text = base_url

    currencies = ET.SubElement(shop, "currencies")
    ET.SubElement(currencies, "currency", {"id": DEFAULT_CURRENCY, "rate": "1"})

    categories_el = ET.SubElement(shop, "categories")
    for category in _used_categories(offers):
        attrs = {"id": str(category.id)}
        rz_id = _category_rz_id(category)
        if rz_id:
            attrs["rz_id"] = rz_id
        ET.SubElement(categories_el, "category", attrs).text = _truncate(category.name, 255)

    offers_el = ET.SubElement(shop, "offers")
    for offer in offers:
        product = offer.product
        offer_el = ET.SubElement(
            offers_el,
            "offer",
            {"id": offer.rozetka_offer_id, "available": "true" if offer.available else "false"},
        )
        ET.SubElement(offer_el, "url").text = _truncate(offer.product_url, 500)
        ET.SubElement(offer_el, "price").text = _format_yml_price(offer.price)
        price_old = offer.old_price if offer.old_price and offer.old_price > offer.price else offer.price
        ET.SubElement(offer_el, "price_old").text = _format_yml_price(price_old)
        ET.SubElement(offer_el, "currencyId").text = DEFAULT_CURRENCY
        ET.SubElement(offer_el, "categoryId").text = str(product.category_id)
        for image_url in offer.image_urls[:20]:
            ET.SubElement(offer_el, "picture").text = _truncate(image_url, 1999)
        ET.SubElement(offer_el, "vendor").text = SHOP_NAME
        ET.SubElement(offer_el, "article").text = _kasta_article_for_product(product)
        ET.SubElement(offer_el, "stock_quantity").text = str(_feed_stock_quantity(offer))

        base_title = _truncate(_clean_xml_text(product.title) or f"{SHOP_NAME} одяг", 255)
        name_ru = _truncate(_kasta_name_ru(base_title) or base_title, 255)
        ET.SubElement(offer_el, "name").text = name_ru
        ET.SubElement(offer_el, "name_ua").text = base_title
        ET.SubElement(offer_el, "name_ru").text = name_ru
        _append_cdata(offer_el, "description", _kasta_description(offer.description_ru))
        _append_cdata(offer_el, "description_ua", _kasta_description(offer.description_ua))
        ET.SubElement(offer_el, "state").text = "new"

        params = [
            ("Колір", offer.color_ua),
            ("Розмір", offer.size),
            ("Склад", offer.material_ua),
            ("Матеріал", offer.material_ua),
            ("Сезон", "Демісезон"),
            ("Стать", "Унісекс"),
            ("Вікова група", "Дорослі"),
            ("Принт", "Є"),
            ("Країна виробництва", "Україна"),
            ("Повернення", "Підлягає поверненню"),
        ]
        for name, value in params:
            clean_value = _truncate(value, 500)
            if clean_value:
                ET.SubElement(offer_el, "param", {"name": name}).text = clean_value

    return _finalize_xml(catalog)


def build_buyme_feed_xml(base_url: str | None = None) -> bytes:
    base_url = resolve_base_url(base_url)
    offers = iter_feed_offers(base_url)

    catalog = ET.Element("yml_catalog", {"date": timezone.now().strftime("%Y-%m-%d %H:%M")})
    shop = ET.SubElement(catalog, "shop")
    ET.SubElement(shop, "name").text = BUYME_SHOP_NAME
    ET.SubElement(shop, "company").text = BUYME_SHOP_NAME

    currencies = ET.SubElement(shop, "currencies")
    ET.SubElement(currencies, "currency", {"id": DEFAULT_CURRENCY, "rate": "1"})

    categories_el = ET.SubElement(shop, "categories")
    for category in _used_categories(offers):
        name = _truncate(_sanitize_buyme_text(category.name) or "Одяг", 255)
        ET.SubElement(categories_el, "category", {"id": str(category.id)}).text = name

    offers_el = ET.SubElement(shop, "offers")
    for offer in offers:
        product = offer.product
        offer_id = _buyme_offer_id(product.id, offer.variant_id, offer.size, offer.color_ua)
        retail_price = _buyme_retail_price(offer)
        drop_price = _buyme_drop_price(product, retail_price)
        quantity = _buyme_quantity(offer)
        offer_el = ET.SubElement(
            offers_el,
            "offer",
            {
                "id": offer_id,
                "available": "true",
                "group_id": _buyme_group_id(offer),
            },
        )
        ET.SubElement(offer_el, "name").text = _buyme_base_name(product)
        ET.SubElement(offer_el, "name_ua").text = _buyme_base_name(product)
        ET.SubElement(offer_el, "price").text = _format_buyme_price(retail_price)
        ET.SubElement(offer_el, "priceDrop").text = _format_buyme_price(drop_price)
        ET.SubElement(offer_el, "currencyId").text = DEFAULT_CURRENCY
        ET.SubElement(offer_el, "categoryId").text = str(product.category_id)

        for image_url in offer.image_urls[:10]:
            ET.SubElement(offer_el, "picture").text = _truncate(image_url, 1999)

        ET.SubElement(offer_el, "vendorCode").text = offer_id
        ET.SubElement(offer_el, "quantity_in_stock").text = str(quantity)
        ET.SubElement(offer_el, "country_of_origin").text = "Україна"
        ET.SubElement(offer_el, "pickup").text = "false"
        ET.SubElement(offer_el, "delivery").text = "true"
        _append_cdata(offer_el, "description", _buyme_description(product, offer))
        _append_cdata(offer_el, "description_ua", _buyme_description(product, offer))

        params = [
            ("Колір", offer.color_ua),
            ("Розмір", offer.size),
            ("Матеріал", offer.material_ua),
            ("Сезон", "Демісезон"),
            ("Стать", "Унісекс"),
            ("Вікова група", "Дорослі"),
            ("Принт", "Є"),
            ("Країна виробництва", "Україна"),
        ]
        for name, value in params[:50]:
            clean_value = _truncate(_sanitize_buyme_text(value), 500)
            if clean_value:
                ET.SubElement(offer_el, "param", {"name": name}).text = clean_value

    return _finalize_xml(catalog)


def _build_merchant_custom_labels(product, offer) -> list[str]:
    """Phase 21 (PR-6, T17.2) — derive five Merchant Center custom_label
    values for one offer. Returns a list of length 5, where empty slots
    are emitted as empty strings (caller filters them out).

    Stable ordering / value enumeration so dashboard saved-segments
    survive rebuilds.
    """
    # 0 — theme keyword resolved from the product (uses the same
    # mapping used by the on-site SEO landing copy so feed labels and
    # SEO content stay consistent).
    try:
        from .product_seo_landing import get_theme_for_product as _theme
        theme_payload = _theme(product) or {}
        theme_key = (theme_payload.get("key") or "").strip().lower()
    except Exception:
        theme_key = ""

    # 1 — category slug.
    category_slug = ""
    if getattr(product, "category", None) is not None:
        category_slug = (getattr(product.category, "slug", "") or "").strip().lower()

    # 2 — price tier.
    try:
        price_int = int(getattr(offer, "price", 0) or 0)
    except (TypeError, ValueError):
        price_int = 0
    if price_int <= 0:
        price_tier = ""
    elif price_int < 500:
        price_tier = "sub_500"
    elif price_int < 1000:
        price_tier = "500_1000"
    else:
        price_tier = "1000_plus"

    # 3 — discount flag.
    is_discounted = bool(getattr(product, "has_discount", False)) and bool(
        getattr(offer, "base_price", 0) and offer.base_price > offer.price
    )
    discount_flag = "on_sale" if is_discounted else "regular"

    # 4 — age cohort. Newly added products (≤90 days) flagged so smart
    # bidding can prioritise them; older listings get ``classic``.
    age_cohort = "classic"
    created_at = getattr(product, "created_at", None) or getattr(product, "published_at", None)
    if created_at is not None:
        try:
            from django.utils import timezone as _tz
            from datetime import timedelta as _td
            if _tz.now() - created_at <= _td(days=90):
                age_cohort = "new_2026"
        except Exception:
            pass

    return [theme_key, category_slug, price_tier, discount_flag, age_cohort]


def build_google_merchant_feed_xml(base_url: str | None = None) -> bytes:
    base_url = resolve_base_url(base_url)
    offers = iter_feed_offers(base_url)

    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "TwoComms - Стріт & Мілітарі Одяг"
    ET.SubElement(channel, "link").text = base_url
    ET.SubElement(channel, "description").text = "Магазин стріт та мілітарі одягу з ексклюзивним дизайном"

    for offer in offers:
        product = offer.product
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, f"{G}id").text = _truncate(offer.google_offer_id, 50)
        ET.SubElement(item, f"{G}item_group_id").text = _truncate(offer.group_id, 50)
        ET.SubElement(item, f"{G}title").text = _truncate(
            f"{product.title} - {offer.color_ua} - {offer.size}",
            150,
        )
        ET.SubElement(item, f"{G}description").text = offer.google_description
        ET.SubElement(item, f"{G}link").text = offer.product_url
        ET.SubElement(item, f"{G}image_link").text = offer.image_urls[0]
        for image_url in offer.image_urls[1:11]:
            ET.SubElement(item, f"{G}additional_image_link").text = image_url
        ET.SubElement(item, f"{G}availability").text = "in_stock" if offer.available else "out_of_stock"

        if getattr(product, "has_discount", False) and offer.base_price > offer.price:
            ET.SubElement(item, f"{G}price").text = _format_google_price(offer.base_price)
            ET.SubElement(item, f"{G}sale_price").text = _format_google_price(offer.price)
        else:
            ET.SubElement(item, f"{G}price").text = _format_google_price(offer.price)

        ET.SubElement(item, f"{G}condition").text = "new"
        ET.SubElement(item, f"{G}brand").text = SHOP_NAME
        ET.SubElement(item, f"{G}mpn").text = _truncate(f"{offer.article}-{product.id}", 70)
        if is_valid_gtin(offer.barcode):
            ET.SubElement(item, f"{G}gtin").text = offer.barcode
        ET.SubElement(item, f"{G}product_type").text = _truncate(getattr(product.category, "name", "Одяг"), 750)
        ET.SubElement(item, f"{G}google_product_category").text = DEFAULT_GOOGLE_PRODUCT_CATEGORY
        ET.SubElement(item, f"{G}age_group").text = "adult"
        ET.SubElement(item, f"{G}gender").text = "unisex"
        ET.SubElement(item, f"{G}size").text = _truncate(offer.size, 100)
        ET.SubElement(item, f"{G}size_system").text = "EU"
        ET.SubElement(item, f"{G}color").text = _truncate(offer.color_ua, 100)
        ET.SubElement(item, f"{G}material").text = _truncate(offer.material_ua, 200)

        if offer.video_link:
            ET.SubElement(item, f"{G}video_link").text = offer.video_link

        # Phase 21 (PR-6, T17.2) — custom_label_0..4 for Merchant
        # Center segmentation. These don't appear in PLAs / shopping
        # results, but power smart-bidding rules and report filters.
        # Order is stable so saved-segments don't drift between feed
        # rebuilds:
        #   0: theme key  (military / streetwear / patriotic / generic)
        #   1: category slug (tshirts / hoodie / long-sleeve / …)
        #   2: price tier (sub_500 / 500_1000 / 1000_plus)
        #   3: discount flag (on_sale / regular)
        #   4: age cohort (new_2026 / classic)
        for idx, label in enumerate(_build_merchant_custom_labels(product, offer)):
            if not label:
                continue
            ET.SubElement(item, f"{G}custom_label_{idx}").text = _truncate(label, 100)

        for highlight in [
            "Ексклюзивний дизайн TwoComms",
            f"Матеріал: {offer.material_ua}",
            "Виробництво: Україна",
            "Підходить для щоденного носіння",
            f"Колір: {offer.color_ua}; розмір: {offer.size}",
        ]:
            ET.SubElement(item, f"{G}product_highlight").text = _truncate(highlight, 150)

        for section, name, value in [
            ("Характеристики", "Бренд", SHOP_NAME),
            ("Характеристики", "Матеріал", offer.material_ua),
            ("Характеристики", "Колір", offer.color_ua),
            ("Характеристики", "Розмір", offer.size),
            ("Характеристики", "Країна виробництва", "Україна"),
            ("Характеристики", "Сезон", "Демісезон"),
        ]:
            detail = ET.SubElement(item, f"{G}product_detail")
            ET.SubElement(detail, f"{G}section_name").text = _truncate(section, 140)
            ET.SubElement(detail, f"{G}attribute_name").text = _truncate(name, 140)
            ET.SubElement(detail, f"{G}attribute_value").text = _truncate(value, 1000)

    ET.indent(rss, space="  ", level=0)
    return ET.tostring(rss, encoding="utf-8", xml_declaration=True)


def _build_yml_feed_xml(*, base_url: str | None, bezzet_mode: bool = False) -> bytes:
    base_url = resolve_base_url(base_url)
    offers = iter_feed_offers(base_url)

    catalog = ET.Element("yml_catalog", {"date": timezone.now().strftime("%Y-%m-%d %H:%M")})
    shop = ET.SubElement(catalog, "shop")
    ET.SubElement(shop, "name").text = SHOP_NAME
    ET.SubElement(shop, "company").text = SHOP_COMPANY
    ET.SubElement(shop, "url").text = base_url
    currencies = ET.SubElement(shop, "currencies")
    ET.SubElement(currencies, "currency", {"id": DEFAULT_CURRENCY, "rate": "1"})
    categories_el = ET.SubElement(shop, "categories")
    for category in _used_categories(offers):
        ET.SubElement(categories_el, "category", {"id": str(category.id)}).text = _truncate(category.name, 255)

    offers_el = ET.SubElement(shop, "offers")
    for offer in offers:
        product = offer.product
        stock_quantity = _bezzet_quantity(offer) if bezzet_mode else _feed_stock_quantity(offer)
        available = True
        group_id = _bezzet_group_id(offer) if bezzet_mode else str(product.id)
        offer_el = ET.SubElement(
            offers_el,
            "offer",
            {
                "id": offer.yml_offer_id,
                "available": "true" if available else "false",
                "group_id": group_id,
            },
        )
        ET.SubElement(offer_el, "url").text = offer.product_url
        ET.SubElement(offer_el, "price").text = _format_yml_price(offer.price)
        if getattr(product, "has_discount", False) and offer.base_price > offer.price:
            ET.SubElement(offer_el, "oldprice").text = _format_yml_price(offer.base_price)
        ET.SubElement(offer_el, "currencyId").text = DEFAULT_CURRENCY
        ET.SubElement(offer_el, "categoryId").text = str(product.category_id)
        for image_url in offer.image_urls[:10]:
            ET.SubElement(offer_el, "picture").text = image_url
        ET.SubElement(offer_el, "name").text = _truncate(f"{product.title} {offer.color_ua} {offer.size}", 255)
        ET.SubElement(offer_el, "name_ua").text = _truncate(f"{product.title} {offer.color_ua} {offer.size}", 255)
        ET.SubElement(offer_el, "vendor").text = SHOP_NAME
        ET.SubElement(offer_el, "vendorCode").text = offer.article
        ET.SubElement(offer_el, "stock_quantity").text = str(stock_quantity)
        if bezzet_mode:
            ET.SubElement(offer_el, "quantity_in_stock").text = str(stock_quantity)
        ET.SubElement(offer_el, "country_of_origin").text = "Україна"
        _append_cdata(offer_el, "description", offer.description_ua)
        _append_cdata(offer_el, "description_ua", offer.description_ua)
        for name, value in [
            ("Розмір", offer.size),
            ("Колір", offer.color_ua),
            ("Матеріал", offer.material_ua),
            ("Сезон", "Демісезон"),
            ("Країна-виробник товару", "Україна"),
        ]:
            ET.SubElement(offer_el, "param", {"name": name}).text = value

    return _finalize_xml(catalog)


def build_uaprom_products_feed_xml(base_url: str | None = None) -> bytes:
    return _build_yml_feed_xml(base_url=base_url, bezzet_mode=True)


def build_prom_feed_xml(base_url: str | None = None) -> bytes:
    return _build_yml_feed_xml(base_url=base_url, bezzet_mode=False)
