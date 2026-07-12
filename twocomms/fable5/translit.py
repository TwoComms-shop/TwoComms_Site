"""
Fable 5 — транслітерація для slug.

Українська: офіційна система КМУ №55 від 27.01.2010 (та сама, що у закордонних
паспортах і в Google-рекомендаціях для укр. URL):
  - ч -> ch, ш -> sh, щ -> shch, х -> kh, ц -> ts, г -> h, ґ -> g
  - є/ї/й/ю/я на початку слова -> ye/yi/y/yu/ya, всередині -> ie/i/i/iu/ia
  - сполучення «зг» -> zgh (щоб відрізняти від «ж» zh)
  - мʼякий знак та апостроф не передаються
Російська: практична латинізація (BGN/PCGN-подібна): ы -> y, э -> e, ё -> yo, ж -> zh.

Це ж відображено дзеркально в JS (static/fable5/editor.js -> f5Translit),
щоб слаг генерувався миттєво при введенні назви.
"""
from __future__ import annotations

import re

# Спільні для укр/рос літери
_COMMON = {
    "а": "a", "б": "b", "в": "v", "д": "d", "е": "e", "ж": "zh", "з": "z",
    "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch",
    "ш": "sh", "щ": "shch", "ь": "", "ъ": "", "’": "", "'": "", "ʼ": "",
}

# Українські специфічні (КМУ-2010). Кортеж: (на початку слова, всередині/в кінці)
_UK_POSITIONAL = {
    "є": ("ye", "ie"),
    "ї": ("yi", "i"),
    "й": ("y", "i"),
    "ю": ("yu", "iu"),
    "я": ("ya", "ia"),
}
_UK = {"г": "h", "ґ": "g", "и": "y", "і": "i"}

# Російські специфічні
_RU = {"г": "g", "и": "i", "й": "y", "ы": "y", "э": "e", "ё": "yo", "ю": "yu", "я": "ya", "е": "e", "є": "e"}

_UK_MARKERS = set("іїєґ")
_RU_MARKERS = set("ыэёъ")

_QUOTES_RE = re.compile(r"[«»„“”\"'’ʼ`]")
_NON_SLUG_RE = re.compile(r"[^a-z0-9]+")


def detect_lang(text: str) -> str:
    """Груба евристика: укр чи рос. За замовчуванням — укр (основна мова сайту)."""
    lowered = (text or "").lower()
    if any(ch in _RU_MARKERS for ch in lowered) and not any(ch in _UK_MARKERS for ch in lowered):
        return "ru"
    return "uk"


def transliterate(text: str, lang: str | None = None) -> str:
    """Транслітерує кирилицю в латиницю з урахуванням позиційних правил КМУ-2010."""
    source = text or ""
    lang = lang or detect_lang(source)
    out: list[str] = []
    word_start = True
    i = 0
    lowered = source.lower()
    while i < len(source):
        ch = source[i]
        low = lowered[i]

        # КМУ-2010: «зг» -> zgh (Згорани -> Zghorany)
        if lang == "uk" and low == "з" and i + 1 < len(source) and lowered[i + 1] == "г":
            out.append("zgh" if ch.islower() else "Zgh")
            i += 2
            word_start = False
            continue

        mapped = None
        if lang == "uk" and low in _UK_POSITIONAL:
            mapped = _UK_POSITIONAL[low][0 if word_start else 1]
        elif lang == "uk" and low in _UK:
            mapped = _UK[low]
        elif lang == "ru" and low in _RU:
            mapped = _RU[low]
        elif low in _COMMON:
            mapped = _COMMON[low]

        if mapped is None:
            out.append(ch)
            word_start = not ch.isalnum()
        else:
            out.append(mapped.capitalize() if (ch.isupper() and mapped) else mapped)
            word_start = False
        i += 1
    return "".join(out)


def smart_slugify(text: str, max_length: int = 80) -> str:
    """Назва (укр/рос/будь-яка) -> чистий англомовний slug.

    - лапки/апострофи видаляються («Бойова квіточка» -> boiova-kvitochka)
    - все не-латинське транслітерується за КМУ-2010
    - пробіли/розділові знаки -> одиночний дефіс, без дефісів по краях
    """
    value = _QUOTES_RE.sub("", text or "")
    value = transliterate(value).lower()
    value = _NON_SLUG_RE.sub("-", value).strip("-")
    value = re.sub(r"-{2,}", "-", value)
    if max_length and len(value) > max_length:
        value = value[:max_length].rstrip("-")
        # не обрізаємо слово посередині, якщо можна
        if "-" in value and len(value) > 40:
            value = value.rsplit("-", 1)[0]
    return value or "tovar"


def unique_product_slug(text: str, exclude_pk=None, max_length: int = 80) -> str:
    """Slug, унікальний серед storefront.Product (як unique_slugify, але з КМУ-2010)."""
    from storefront.models import Product

    base = smart_slugify(text, max_length=max_length)
    qs = Product.objects.all()
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    candidate = base
    index = 2
    while qs.filter(slug=candidate).exists():
        suffix = f"-{index}"
        candidate = f"{base[: max_length - len(suffix)].rstrip('-')}{suffix}"
        index += 1
    return candidate
