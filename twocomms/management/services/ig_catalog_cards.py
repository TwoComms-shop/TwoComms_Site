"""Э1.5 — карточки каталогу і вибору розміру.

Contract of record: `docs/instagram_bot_audit/new/10_VISUAL_MESSAGING.md` §5–§6.
Тут — тільки implementation: cardinality, paging, підбір фото і набір кнопок.

**Чому окремий модуль, а не четверта форма карточки.** Форми доставки вже є в
`ig_message_templates` (`GenericTemplate`, `ButtonTemplate`, `QuickReplyMessage`)
і в них уже вкладені ліміти Meta, деградація полів і receipt-first транспорт.
Цей модуль нічого не відправляє: він **вирішує**, яку з наявних форм заповнити
даними каталогу і якими кнопками. Плани — чисті значення, тому їх можна перевірити
тестом без провайдера й без БД.

**Чому планувальник не вмикає карточки сам.** Кожна карточка має власний
feature-флаг (`Откат` розділу Э1.5), і диспетчери `plan_catalog_visual()` /
`plan_size_visual()` повертають `None`, коли флаг вимкнений. `None` означає
«карточки немає — іде звичайний текстовий шлях», а не «карточка без кнопок».
Мовчазна деградація до карточки без дії — гірший з можливих результатів: питання
поставлене, відповісти нічим.

**Чого тут свідомо немає.** Переходу стану по натисканню. Э1.4 лишила
`product/variant/size` як `[~]` (signed postback V2 ще немає), а Э1.13 має цей
крок у себе в списку («Product/size/payment/consent/ТТН actions переходять у
`NO_MODEL` router»). Тому тут є `parse_card_action()` — детермінований розбір
payload у типізований намір — і немає жодного запису в комерційний стан.
Роутер Э1.4 на невідому дію повертає `None`, тобто хід іде звичайним шляхом:
поки флаг карточки вимкнений, payload такої форми фізично не може прийти.
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from management.services import ig_message_templates as tpl

# --- Feature-флаги: по одному на карточку ------------------------------------
# Гранульованість саме така, як в «Откат: feature-флаг per-карточка»: розмірна
# сітка може бути готова, а карусель — ні, і вимикати доводиться щось одне.
FLAG_CATEGORY_CAROUSEL = "IG_CARD_CATEGORY_CAROUSEL"
FLAG_PRODUCT_CAROUSEL = "IG_CARD_PRODUCT_CAROUSEL"
FLAG_SIZE_CHOICE = "IG_CARD_SIZE_CHOICE"
FLAG_SIZE_GRID = "IG_CARD_SIZE_GRID"

# Default off: production enablement карточок gated Э1.13, а не цим модулем.
CARD_FLAGS = (
    FLAG_CATEGORY_CAROUSEL,
    FLAG_PRODUCT_CAROUSEL,
    FLAG_SIZE_CHOICE,
    FLAG_SIZE_GRID,
)


def card_enabled(flag_name: str) -> bool:
    """Чи дозволена ця карточка. Читає той самий флаг-хелпер, що й решта етапів."""
    from management.services.ig_provider_incidents import flag

    return flag(flag_name, False)


KIND_TEXT = "text"
KIND_SINGLE_CARD = "single_card"
KIND_CAROUSEL = "carousel"
KIND_QUICK_REPLIES = "quick_replies"
KIND_SIZE_GRID_CARD = "size_grid_card"

# Сторінка каруселі — три елементи. Ліміт Meta (10) тут навмисно не при чому:
# він визначає, скільки можна, а не скільки має сенс. Понад три елементи клієнт
# уже не порівнює, а гортає.
PAGE_SIZE = 3
# Скільком id зберігаємо порядок. Обрізка можлива, але вона видима
# (`truncated`/`total` у стані), бо «тихо викинути залишок» — це і є та помилка,
# від якої існує digest.
MAX_TRACKED_CANDIDATES = 200

# --- Payload-и: `<domain>:<generation>:<verb>[:<value>]` у версіонованому ns --
# Э1.4 задала форму `size:14:set:L`, а `ig_message_templates.build_payload`
# додає обов'язковий версіонований префікс `twc:1:`. Разом виходить
# `twc:1:size:14:set:L`: версія схеми лишається зовні (без неї натискання на
# старій карточці виконало б не ту дію, яку клієнт бачив), а domain/generation/
# verb — усередині, як у документі.
ACTION_PRODUCT = "product"
ACTION_CATEGORY = "category"
ACTION_CATALOG = "catalog"
ACTION_SIZE = "size"
CARD_ACTIONS = (ACTION_PRODUCT, ACTION_CATEGORY, ACTION_CATALOG, ACTION_SIZE)

VERB_PICK = "pick"
VERB_MORE = "more"
VERB_SET = "set"
VERB_REOPEN = "reopen"
VERB_ASK = "ask"
VERB_GRID = "grid"


def _generation_text(value) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("card payload requires an integer generation") from exc
    if number < 0:
        raise ValueError("card payload requires a non-negative generation")
    return str(number)


def _positive_id(value) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("card payload requires a positive id") from exc
    if number <= 0:
        raise ValueError("card payload requires a positive id")
    return str(number)


def product_pick_payload(generation, product_id: int) -> str:
    """`twc:1:product:<gen>:pick:<product_id>` — однозначний товар, не «отой»."""
    return tpl.build_payload(
        ACTION_PRODUCT, _generation_text(generation), VERB_PICK, _positive_id(product_id)
    )


def category_pick_payload(generation, category_id: int) -> str:
    return tpl.build_payload(
        ACTION_CATEGORY, _generation_text(generation), VERB_PICK, _positive_id(category_id)
    )


def catalog_more_payload(generation, next_page: int) -> str:
    """`Показати ще` несе номер НАСТУПНОЇ сторінки, а не «ще трохи».

    Без номера сторінки повторне натискання на тій самій карточці показало б ту
    саму трійку, і клієнт вирішив би, що асортимент закінчився.
    """
    try:
        page = int(next_page)
    except (TypeError, ValueError) as exc:
        raise ValueError("catalog paging requires an integer page") from exc
    if page < 1:
        raise ValueError("the first page needs no `more` button")
    return tpl.build_payload(ACTION_CATALOG, _generation_text(generation), VERB_MORE, str(page))


def size_set_payload(generation, size: str) -> str:
    value = str(size or "").strip().upper()
    if not value:
        raise ValueError("size payload requires a size")
    return tpl.build_payload(ACTION_SIZE, _generation_text(generation), VERB_SET, value)


def size_reopen_payload(generation) -> str:
    """Кнопка `Обрати розмір` на розмірній сітці — повернення до вибору."""
    return tpl.build_payload(ACTION_SIZE, _generation_text(generation), VERB_REOPEN)


def size_question_payload(generation) -> str:
    """`Питання` — клієнт хоче поговорити про замір, а не обрати навпомацки."""
    return tpl.build_payload(ACTION_SIZE, _generation_text(generation), VERB_ASK)


def size_grid_payload(generation) -> str:
    """`Таблиця розмірів` — показати сітку замірів для обраного крою."""
    return tpl.build_payload(ACTION_SIZE, _generation_text(generation), VERB_GRID)


@dataclass(frozen=True)
class CardAction:
    """Розібране натискання: домен, генерація карточки, дія і значення."""

    domain: str
    generation: int
    verb: str
    value: str = ""


def parse_card_action(raw: str) -> CardAction | None:
    """Розібрати payload карточки або повернути `None`.

    `None` — це не помилка, а «це не наша кнопка»: рівно той контракт, який Э1.4
    вже має для невідомої дії. Невірна генерація тут НЕ відкидається: свіжість
    перевіряє той, хто знає поточний стан, і його відмова мусить бути мʼякою
    («ось актуальні варіанти»), а не тишею.
    """
    parsed = tpl.parse_payload(raw)
    if not parsed:
        return None
    domain = str(parsed.get("action") or "")
    if domain not in CARD_ACTIONS:
        return None
    args = tuple(str(value or "") for value in parsed.get("args") or ())
    if len(args) < 2 or not args[0].isdigit():
        return None
    verb = args[1]
    if not verb:
        return None
    return CardAction(
        domain=domain,
        generation=int(args[0]),
        verb=verb,
        value=args[2] if len(args) > 2 else "",
    )


@dataclass(frozen=True)
class CardPlan:
    """Що саме буде надіслано і що про це треба записати.

    Плану достатньо, щоб (а) відправити через наявний транспорт, (б) записати
    показане в `record_shown_products`-сумісному вигляді, (в) побачити в логу,
    чому фото generic, і (г) знати, чи лишився залишок кандидатів.
    """

    kind: str
    payload: object
    reason: str
    lang: str = "uk"
    generation: int = 0
    page: int = 0
    page_size: int = PAGE_SIZE
    total_candidates: int = 0
    has_more: bool = False
    revision: int = 0
    digest: str = ""
    # (position, product_id, title) у тому самому порядку, в якому бачить клієнт.
    shown: tuple = ()
    media_fallback_reason: str = ""
    available_sizes: tuple = ()
    disabled_sizes: tuple = ()
    missing_product_ids: tuple = ()

    @property
    def shown_product_ids(self) -> tuple:
        return tuple(int(entry[1]) for entry in self.shown)

    @property
    def fallback_text(self) -> str:
        if isinstance(self.payload, str):
            return self.payload
        return str(getattr(self.payload, "fallback_text", "") or "")


@dataclass(frozen=True)
class CategoryOption:
    """Категорія для broad-browse каруселі: id, підпис і (можливо) фото."""

    category_id: int
    label: str
    slug: str = ""
    image_url: str = ""


_LANGS = ("uk", "ru", "en")


def _lang(value: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in _LANGS else "uk"


_FIT_LABELS = {
    "oversize": {"uk": "Оверсайз", "ru": "Оверсайз", "en": "Oversize"},
    "classic": {"uk": "Класика", "ru": "Классика", "en": "Classic"},
}
_CURRENCY = {"uk": "₴", "ru": "₴", "en": "UAH"}
_FROM_PREFIX = {"uk": "від", "ru": "от", "en": "from"}

# Підпис кнопки розміру беремо зі словника `BUTTON_LABELS`, а порядок розмірів —
# з сітки товару. Сортувати тут по-своєму не можна: клієнт бачить на сайті
# порядок каталогу, і власний порядок у кнопках читався б як інший набір.
SIZE_LABEL_KEYS = {
    "XS": "size_xs", "S": "size_s", "M": "size_m", "L": "size_l",
    "XL": "size_xl", "XXL": "size_xxl", "XXXL": "size_xxxl",
}


def _label(key: str, lang: str) -> str:
    return tpl.button_label(key, lang)


def _size_label(size: str, lang: str) -> str:
    value = str(size or "").strip().upper()
    return _label(SIZE_LABEL_KEYS.get(value, ""), lang) or value


def _base_url() -> str:
    from management.services.ig_catalog_media import _base_url as media_base_url

    return media_base_url()


def product_url(slug: str) -> str:
    """Абсолютний HTTPS-URL сторінки товару для кнопки `Детальніше`."""
    clean = str(slug or "").strip("/")
    return f"{_base_url()}/product/{clean}/" if clean else _base_url()


def _grouped_amount(value) -> str:
    """`1250` → `1 250`: у subtitle немає сітки сайту, де розряди й так видно."""
    from management.services.ig_catalog_pricing import money_text

    digits = money_text(value)
    whole, _, fraction = digits.partition(".")
    chunks = []
    while len(whole) > 3:
        chunks.insert(0, whole[-3:])
        whole = whole[:-3]
    chunks.insert(0, whole)
    grouped = " ".join(chunks)
    return f"{grouped}.{fraction}" if fraction else grouped


def _fit_summary(pricing, lang: str) -> str:
    """«Оверсайз/класика» — з реальних конфігурацій, а не з припущення про крій."""
    codes: list = []
    for configuration in tuple(getattr(pricing, "configurations", ()) or ()):
        code = str(getattr(configuration, "fit_code", "") or "").strip().lower()
        if code and code not in codes:
            codes.append(code)
    labels = [
        _FIT_LABELS.get(code, {}).get(lang, "") or code.capitalize()
        for code in codes
    ]
    labels = [value for value in labels if value]
    if not labels:
        return ""
    return "/".join(
        value if index == 0 else value.lower() for index, value in enumerate(labels)
    )


def _price_summary(pricing, lang: str) -> str:
    """«від 1 250 ₴». Порожній рядок — валідний результат.

    Карточка без ціни краща за карточку з вигаданою: §6 однаково вимагає
    перепровірити price безпосередньо перед кожним `Обрати`.
    """
    minimum = getattr(pricing, "minimum", None)
    maximum = getattr(pricing, "maximum", None)
    amount = minimum if minimum is not None else maximum
    if amount is None:
        return ""
    unit = _CURRENCY[lang]
    exact = bool(getattr(pricing, "exact", False)) or minimum == maximum
    if exact:
        return f"{_grouped_amount(amount)} {unit}"
    return f"{_FROM_PREFIX[lang]} {_grouped_amount(amount)} {unit}"


def product_subtitle(candidate, lang: str = "uk") -> str:
    """`«Оверсайз/класика · від 1 250 ₴»` — рівно те, що вимагає Э1.5."""
    lang = _lang(lang)
    parts = [
        _fit_summary(getattr(candidate, "pricing", None), lang),
        _price_summary(getattr(candidate, "pricing", None), lang),
    ]
    return " · ".join(part for part in parts if part)


def _product_card(candidate, *, lang: str, generation, image_url: str = ""):
    """Елемент каруселі: фото варіанта, коротка назва, крій і ціна, дві кнопки.

    `default_url` дублює `Детальніше` свідомо: тап по самій карточці — найчастіший
    жест, і без `default_action` він не робить нічого.
    """
    url = product_url(getattr(candidate, "slug", ""))
    return tpl.TemplateCard(
        title=tpl.display_short(str(getattr(candidate, "title", "") or "")),
        subtitle=product_subtitle(candidate, lang),
        image_url=str(image_url or ""),
        default_url=url,
        buttons=(
            tpl.TemplateButton(
                tpl.BUTTON_POSTBACK,
                _label("product_select", lang),
                payload=product_pick_payload(generation, getattr(candidate, "product_id", 0)),
            ),
            tpl.TemplateButton(tpl.BUTTON_WEB_URL, _label("product_details", lang), url=url),
        ),
    )


_CONSTRAINT_LABELS = {
    "color": {"uk": "колір", "ru": "цвет", "en": "colour"},
    "colour": {"uk": "колір", "ru": "цвет", "en": "colour"},
    "size": {"uk": "розмір", "ru": "размер", "en": "size"},
    "size_code": {"uk": "розмір", "ru": "размер", "en": "size"},
    "fit": {"uk": "крій", "ru": "крой", "en": "fit"},
    "cut": {"uk": "крій", "ru": "крой", "en": "fit"},
    "style": {"uk": "крій", "ru": "крой", "en": "fit"},
    "garment_type": {"uk": "тип", "ru": "тип", "en": "type"},
    "category": {"uk": "категорія", "ru": "категория", "en": "category"},
}
# Способи послабити фільтр названі по тому обмеженню, яке його створило: «інший
# колір» має сенс лише коли колір справді був у запиті. Загальна підказка
# («схожі принти») лишається останньою, а не першою.
_RELAX_HINTS = {
    "color": {"uk": "інший колір", "ru": "другой цвет", "en": "another colour"},
    "colour": {"uk": "інший колір", "ru": "другой цвет", "en": "another colour"},
    "size": {"uk": "інший розмір", "ru": "другой размер", "en": "another size"},
    "size_code": {"uk": "інший розмір", "ru": "другой размер", "en": "another size"},
    "fit": {"uk": "інший крій", "ru": "другой крой", "en": "another fit"},
    "cut": {"uk": "інший крій", "ru": "другой крой", "en": "another fit"},
    "style": {"uk": "інший крій", "ru": "другой крой", "en": "another fit"},
    "garment_type": {"uk": "інший тип одягу", "ru": "другой тип одежды", "en": "another garment"},
    "category": {"uk": "інша категорія", "ru": "другая категория", "en": "another category"},
}
_GENERIC_HINT = {"uk": "схожі принти", "ru": "похожие принты", "en": "similar prints"}
MAX_RELAX_HINTS = 3

_ZERO_HEAD = {
    "uk": "За таким запитом ({filters}) зараз нічого немає.",
    "ru": "По такому запросу ({filters}) сейчас ничего нет.",
    "en": "Nothing matches that request ({filters}) right now.",
}
_ZERO_HEAD_NO_FILTERS = {
    "uk": "За таким запитом зараз нічого немає.",
    "ru": "По такому запросу сейчас ничего нет.",
    "en": "Nothing matches that request right now.",
}
_ZERO_TAIL = {
    "uk": "Можу пошукати так: {hints}. Скажіть, що підходить.",
    "ru": "Могу поискать так: {hints}. Скажите, что подходит.",
    "en": "I can look at: {hints}. Tell me which works.",
}


def _normalized_constraints(constraints) -> tuple:
    """`{"color": "black"}` або `(("color", "black"),)` → стабільний кортеж пар."""
    if constraints is None:
        return ()
    items = (
        constraints.items()
        if isinstance(constraints, Mapping)
        else (constraints if isinstance(constraints, Sequence) else ())
    )
    pairs: list = []
    for entry in items:
        try:
            key, value = entry
        except (TypeError, ValueError):
            continue
        key = str(key or "").strip()
        value = str(value or "").strip()
        if key and value:
            pairs.append((key, value))
    return tuple(pairs)


def zero_candidates_text(constraints, lang: str = "uk") -> str:
    """Честний текст: які саме обмеження не дали результату і що послабити.

    Назви лише ті обмеження, які справді були в запиті. «Спробуйте інший колір»
    у запиті без кольору — це вигадка, і клієнт це бачить.
    """
    lang = _lang(lang)
    pairs = _normalized_constraints(constraints)
    hints: list = []
    for key, _value in pairs:
        hint = _RELAX_HINTS.get(key, {}).get(lang, "")
        if hint and hint not in hints:
            hints.append(hint)
    generic = _GENERIC_HINT[lang]
    if generic not in hints:
        hints.append(generic)
    hints = hints[:MAX_RELAX_HINTS]
    if pairs:
        filters = ", ".join(
            f"{_CONSTRAINT_LABELS.get(key, {}).get(lang, key)}: {value}"
            for key, value in pairs
        )
        opening = _ZERO_HEAD[lang].format(filters=filters)
    else:
        opening = _ZERO_HEAD_NO_FILTERS[lang]
    return f"{opening} {_ZERO_TAIL[lang].format(hints=', '.join(hints))}"


_PAGE_FALLBACK = {
    "uk": "Ось що підходить: {items}.",
    "ru": "Вот что подходит: {items}.",
    "en": "Here is what matches: {items}.",
}
_PAGE_REMAINDER = {
    "uk": " Це {shown} з {total} — напишіть «ще», і покажу наступні.",
    "ru": " Это {shown} из {total} — напишите «ещё», и покажу следующие.",
    "en": " That is {shown} of {total} — say \"more\" and I will show the rest.",
}


def _page_fallback_text(window, *, lang: str, shown_total: int, total: int) -> str:
    """Текстовий еквівалент сторінки — з чесним «це N з M».

    Шаблон у web-версії Instagram не рендериться взагалі, тому текст мусить
    нести той самий смисл. І він мусить називати залишок: три товари без
    згадки про решту читаються як «це весь асортимент» (§6).
    """
    items = ", ".join(
        str(getattr(candidate, "title", "") or "").strip()
        for candidate in window
        if str(getattr(candidate, "title", "") or "").strip()
    )
    text = _PAGE_FALLBACK[lang].format(items=items)
    if total > shown_total:
        text += _PAGE_REMAINDER[lang].format(shown=shown_total, total=total)
    return text


def plan_product_cards(
    candidates,
    *,
    lang: str = "uk",
    generation=0,
    page: int = 0,
    page_size: int = PAGE_SIZE,
    images=None,
    constraints=None,
    revision: int = 0,
    media_fallback_reason: str = "",
) -> CardPlan:
    """Обов'язкова cardinality matrix §6: `0` / `1` / `2–3` / `4+`.

    `candidates` — ПОВНИЙ упорядкований список, а не сторінка: інакше «4+»
    неможливо відрізнити від «3», і `Показати ще` не мав би що показувати.
    Ліміт Meta у 10 елементів тут не використовується як UX-ціль: сторінка — три.
    """
    lang = _lang(lang)
    ordered = tuple(candidates or ())
    total = len(ordered)
    size = max(1, int(page_size or PAGE_SIZE))
    index = max(0, int(page or 0))
    if total == 0:
        return CardPlan(
            kind=KIND_TEXT,
            payload=zero_candidates_text(constraints, lang),
            reason="no_candidates",
            lang=lang,
            generation=int(generation or 0),
            total_candidates=0,
            revision=int(revision or 0),
        )

    window = ordered[index * size : index * size + size]
    if not window:
        # Сторінка за межами списку — це не «нічого немає», а застаріле
        # натискання. Мовчати не можна: клієнт натиснув.
        return CardPlan(
            kind=KIND_TEXT,
            payload=_page_fallback_text(
                ordered[:size], lang=lang, shown_total=min(size, total), total=total
            ),
            reason="page_out_of_range",
            lang=lang,
            generation=int(generation or 0),
            page=index,
            page_size=size,
            total_candidates=total,
        )

    shown_total = index * size + len(window)
    has_more = shown_total < total
    image_map = dict(images or {})
    cards = tuple(
        _product_card(
            candidate,
            lang=lang,
            generation=generation,
            image_url=image_map.get(int(getattr(candidate, "product_id", 0) or 0), ""),
        )
        for candidate in window
    )
    quick_replies = ()
    if has_more:
        quick_replies = (
            tpl.QuickReply(
                _label("catalog_more", lang), catalog_more_payload(generation, index + 1)
            ),
        )
    fallback = _page_fallback_text(
        window, lang=lang, shown_total=shown_total, total=total
    )
    # Карусель з одного елемента заборонена (§6). У Meta один елемент і є single
    # card — різниця не у формі на дроті, а в тому, що ми НЕ дописуємо до неї
    # `Показати ще` і не називаємо це вибором.
    kind = KIND_SINGLE_CARD if len(window) == 1 else KIND_CAROUSEL
    return CardPlan(
        kind=kind,
        payload=tpl.GenericTemplate(
            cards=cards, fallback_text=fallback, quick_replies=quick_replies
        ),
        reason=f"candidates:{total}:page:{index}",
        lang=lang,
        generation=int(generation or 0),
        page=index,
        page_size=size,
        total_candidates=total,
        has_more=has_more,
        revision=int(revision or 0),
        digest=candidate_digest(
            int(getattr(candidate, "product_id", 0) or 0) for candidate in ordered
        ),
        shown=tuple(
            (
                offset + 1,
                int(getattr(candidate, "product_id", 0) or 0),
                str(getattr(candidate, "title", "") or ""),
            )
            for offset, candidate in enumerate(window)
        ),
        media_fallback_reason=str(media_fallback_reason or ""),
    )


_CATEGORY_FALLBACK = {
    "uk": "Є: {items} — що дивимо?",
    "ru": "Есть: {items} — что смотрим?",
    "en": "We have: {items} — what are we looking at?",
}
_CATEGORY_EMPTY = {
    "uk": "Скажіть, що шукаєте — футболку, худі чи лонгслів, — і я підберу.",
    "ru": "Скажите, что ищете — футболку, худи или лонгслив, — и я подберу.",
    "en": "Tell me what you are after — a tee, a hoodie or a longsleeve.",
}


def plan_category_carousel(
    categories,
    *,
    lang: str = "uk",
    generation=0,
    page_size: int = PAGE_SIZE,
) -> CardPlan:
    """Категорії — тільки для broad browse. Порожній список дає текст, не карточку."""
    lang = _lang(lang)
    options = [
        option
        for option in tuple(categories or ())
        if int(getattr(option, "category_id", 0) or 0) > 0
        and str(getattr(option, "label", "") or "").strip()
    ]
    if not options:
        return CardPlan(
            kind=KIND_TEXT,
            payload=_CATEGORY_EMPTY[lang],
            reason="no_categories",
            lang=lang,
            generation=int(generation or 0),
        )
    window = options[: max(1, int(page_size or PAGE_SIZE))]
    cards = tuple(
        tpl.TemplateCard(
            title=tpl.display_short(str(option.label)),
            image_url=str(getattr(option, "image_url", "") or ""),
            buttons=(
                tpl.TemplateButton(
                    tpl.BUTTON_POSTBACK,
                    _label("product_select", lang),
                    payload=category_pick_payload(generation, option.category_id),
                ),
            ),
        )
        for option in window
    )
    fallback = _CATEGORY_FALLBACK[lang].format(
        items=", ".join(str(option.label).strip() for option in window)
    )
    return CardPlan(
        kind=KIND_CAROUSEL,
        payload=tpl.GenericTemplate(cards=cards, fallback_text=fallback),
        reason=f"categories:{len(window)}",
        lang=lang,
        generation=int(generation or 0),
        page_size=max(1, int(page_size or PAGE_SIZE)),
        total_candidates=len(options),
    )


VISUAL_CATEGORY = "category"
VISUAL_PRODUCT = "product"
_GARMENT_KEYS = ("garment_type", "category")


def catalog_visual_kind(*, constraints=None, broad_browse: bool = False) -> str:
    """Category чи product carousel. Це не питання смаку, а заборона з §6.

    Якщо garment уже названий («футболки Харкова»), category carousel
    **заборонена**: показувати категорії тому, хто вже сказав категорію, означає
    перепитати те, що ми знаємо, і перетворити діалог у меню. Категорії потрібні
    лише при реально невідомому garment або прямому «що у вас є».
    """
    pairs = dict(_normalized_constraints(constraints))
    if any(pairs.get(key) for key in _GARMENT_KEYS):
        return VISUAL_PRODUCT
    return VISUAL_CATEGORY if broad_browse else VISUAL_PRODUCT


def plan_catalog_visual(
    *,
    candidates=(),
    categories=(),
    constraints=None,
    broad_browse: bool = False,
    lang: str = "uk",
    generation=0,
    page: int = 0,
    page_size: int = PAGE_SIZE,
    images=None,
    revision: int = 0,
    media_fallback_reason: str = "",
) -> CardPlan | None:
    """Єдиний вхід для каталогу. `None` = карточка вимкнена, іде текстовий шлях."""
    kind = catalog_visual_kind(constraints=constraints, broad_browse=broad_browse)
    if kind == VISUAL_CATEGORY:
        if not card_enabled(FLAG_CATEGORY_CAROUSEL):
            return None
        return plan_category_carousel(
            categories, lang=lang, generation=generation, page_size=page_size
        )
    if not card_enabled(FLAG_PRODUCT_CAROUSEL):
        return None
    return plan_product_cards(
        candidates,
        lang=lang,
        generation=generation,
        page=page,
        page_size=page_size,
        images=images,
        constraints=constraints,
        revision=revision,
        media_fallback_reason=media_fallback_reason,
    )


# --- Ordered candidate digest і cursor ---------------------------------------
# Живе в `IgClient.sales_context` — тому самому JSON, де вже лежить
# `shown_products`. Окрема таблиця тут не потрібна: стан живе рівно один
# діалоговий хід, і міграція заради нього була б дорожчою за факт.
CATALOG_PAGE_CONTEXT_KEY = "catalog_page"


def candidate_digest(product_ids) -> str:
    """Стабільний відпечаток ПОРЯДКУ кандидатів, а не набору.

    Саме порядок: `[7, 3]` і `[3, 7]` — різні показані сторінки, і `Показати ще`
    на одному з них не має права допейджити інший.
    """
    ordered = []
    for value in product_ids or ():
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            ordered.append(str(number))
    if not ordered:
        return ""
    return hashlib.sha256(",".join(ordered).encode("utf-8")).hexdigest()


def _page_state_payload(*, ordered, generation, page, page_size, revision) -> dict:
    from django.utils import timezone

    kept = [int(value) for value in ordered][:MAX_TRACKED_CANDIDATES]
    return {
        "generation": int(generation or 0),
        "digest": candidate_digest(ordered),
        "ordered": kept,
        "total": len(tuple(ordered)),
        "truncated": len(tuple(ordered)) > len(kept),
        "page": int(page),
        "page_size": int(page_size),
        "revision": int(revision),
        # Час потрібен для розбору «чому натискання застаріле»: сам по собі
        # digest каже, що список інший, але не каже, наскільки давно.
        "at": timezone.now().isoformat(),
    }


def candidate_page_state(client) -> dict:
    context = getattr(client, "sales_context", None)
    if not isinstance(context, dict):
        return {}
    state = context.get(CATALOG_PAGE_CONTEXT_KEY)
    return dict(state) if isinstance(state, dict) else {}


def _store_page_state(client, state: dict) -> dict:
    context = dict(getattr(client, "sales_context", {}) or {})
    context[CATALOG_PAGE_CONTEXT_KEY] = state
    client.sales_context = context
    if getattr(client, "pk", None):
        client.save(update_fields=["sales_context", "updated_at"])
    return state


def open_candidate_page(client, product_ids, *, generation=0, page_size: int = PAGE_SIZE) -> dict:
    """Зберегти ПОВНИЙ упорядкований digest ДО показу першої сторінки.

    Порядок саме такий не для акуратності. Якщо зберігати після відправки, то
    між показом і натисканням `Показати ще` немає жодного запису про залишок —
    і другу сторінку доводиться вигадувати новим ранжуванням, яке вже може дати
    інший порядок. Тоді клієнт побачить той самий товар двічі й не побачить
    інший ні разу.
    """
    ordered = [
        int(value)
        for value in (product_ids or ())
        if str(value).strip().lstrip("-").isdigit() and int(value) > 0
    ]
    return _store_page_state(
        client,
        _page_state_payload(
            ordered=ordered,
            generation=generation,
            page=0,
            page_size=max(1, int(page_size or PAGE_SIZE)),
            revision=1,
        ),
    )


def advance_candidate_page(client, *, digest: str = "", generation=None) -> tuple:
    """`Показати ще` → наступна сторінка як НОВА візуальна ревізія.

    Повертає `(state, reason)`. Порожній `state` означає мʼяку відмову з
    названою причиною, а не тишу: `stale_digest` (карточка стосується іншого
    списку), `stale_generation` (стан змінився після показу) або
    `no_more_candidates` (залишку немає). Тихо показати «наступні три» з іншого
    списку — гірше, ніж сказати, що список змінився.
    """
    state = candidate_page_state(client)
    if not state or not state.get("ordered"):
        return {}, "no_page_state"
    if digest and str(state.get("digest") or "") != str(digest):
        return {}, "stale_digest"
    if generation is not None and int(state.get("generation") or 0) != int(generation):
        return {}, "stale_generation"
    size = max(1, int(state.get("page_size") or PAGE_SIZE))
    page = int(state.get("page") or 0) + 1
    ordered = [int(value) for value in state.get("ordered") or ()]
    if page * size >= len(ordered):
        return {}, "no_more_candidates"
    advanced = dict(state)
    advanced["page"] = page
    # Нова ревізія, а не «та сама карточка з іншим вмістом»: натискання на
    # попередній сторінці після цього має бути видимо застарілим.
    advanced["revision"] = int(state.get("revision") or 1) + 1
    from django.utils import timezone

    advanced["at"] = timezone.now().isoformat()
    return _store_page_state(client, advanced), ""


def page_product_ids(state: Mapping) -> tuple:
    """Id саме тієї сторінки, яку описує стан, у збереженому порядку."""
    ordered = [int(value) for value in (state or {}).get("ordered") or ()]
    size = max(1, int((state or {}).get("page_size") or PAGE_SIZE))
    page = max(0, int((state or {}).get("page") or 0))
    return tuple(ordered[page * size : page * size + size])


def select_page(candidates, ordered_product_ids) -> tuple:
    """Кандидати під збережений порядок id + ті id, що зникли з каталогу.

    Товар, який між сторінками зняли з публікації, не має пропасти молча:
    його id повертається окремо, щоб текст міг сказати правду, а не показати
    два елементи там, де обіцяно три.
    """
    by_id = {
        int(getattr(candidate, "product_id", 0) or 0): candidate
        for candidate in tuple(candidates or ())
    }
    found: list = []
    missing: list = []
    for product_id in tuple(ordered_product_ids or ()):
        candidate = by_id.get(int(product_id))
        if candidate is None:
            missing.append(int(product_id))
        else:
            found.append(candidate)
    return tuple(found), tuple(missing)


# --- Вибір розміру -----------------------------------------------------------
# Найбільший виграш розділу і найдорожча помилка. Кнопка недоступного розміру —
# це обіцянка, яку ми не виконаємо, і клієнт дізнається про це вже після вибору.
# Тому джерело набору тут одне: `checkout_readiness()['size']`, той самий, що
# вирішує, чи можна оформити замовлення. Він уже відняв `_disabled_sizes()`.
_SIZE_SUBTITLE = {
    "uk": "Оберіть розмір · в наявності: {sizes}",
    "ru": "Выберите размер · в наличии: {sizes}",
    "en": "Pick a size · in stock: {sizes}",
}
_SIZE_CARD_TITLE = {
    "uk": "Ваш розмір",
    "ru": "Ваш размер",
    "en": "Your size",
}
_SIZE_QUICK_TEXT = {
    "uk": "В наявності: {sizes}. Який ваш?",
    "ru": "В наличии: {sizes}. Какой ваш?",
    "en": "In stock: {sizes}. Which one is yours?",
}
_SIZE_ONE_ONLY = {
    "uk": "Зараз у наявності лише {size} — беремо його?",
    "ru": "Сейчас в наличии только {size} — берём его?",
    "en": "Only {size} is in stock right now — shall we take it?",
}
_SIZE_NONE_WITH_DISABLED = {
    "uk": "Розміри {sizes} зараз вимкнені, інших у сітці немає. Уточню в менеджера і повернуся.",
    "ru": "Размеры {sizes} сейчас отключены, других в сетке нет. Уточню у менеджера и вернусь.",
    "en": "Sizes {sizes} are switched off and the grid has no others. I will check with the manager.",
}
_SIZE_NONE = {
    "uk": "Наявних розмірів у каталозі зараз не видно — уточню і повернуся.",
    "ru": "Доступных размеров в каталоге сейчас не видно — уточню и вернусь.",
    "en": "The catalogue does not show sizes in stock right now — I will check and come back.",
}


def _sizes(values, *, exclude=()) -> tuple:
    excluded = {str(value or "").strip().upper() for value in exclude}
    kept: list = []
    for value in values or ():
        size = str(value or "").strip().upper()
        if size and size not in kept and size not in excluded:
            kept.append(size)
    return tuple(kept)


def readiness_sizes(readiness) -> tuple:
    """`(available, disabled)` з readiness, з відкинутими вимкненими розмірами.

    Фільтр `exclude=disabled` тут — не дублювання `sizes_for_fit()`, а другий
    замок: якщо колись у readiness просочиться вимкнений розмір, він не стане
    кнопкою. Ціна помилки асиметрична, тому перевірка стоїть у двох місцях.
    """
    state = (readiness or {}).get("size") or {}
    disabled = _sizes(state.get("disabled"))
    return _sizes(state.get("available"), exclude=disabled), disabled


def _readiness_fit_label(readiness, lang: str) -> str:
    state = (readiness or {}).get("fit") or {}
    selected = str(state.get("selected") or "").strip().lower()
    if not selected:
        return ""
    for option in state.get("options") or ():
        if str((option or {}).get("code") or "").strip().lower() == selected:
            label = str((option or {}).get("label") or "").strip()
            if label:
                return label
    return _FIT_LABELS.get(selected, {}).get(lang, "") or selected.capitalize()


def _size_buttons(sizes, *, lang: str, generation) -> tuple:
    return tuple(
        tpl.TemplateButton(
            tpl.BUTTON_POSTBACK,
            _size_label(size, lang),
            payload=size_set_payload(generation, size),
        )
        for size in sizes
    )


def plan_size_choice(
    readiness,
    *,
    lang: str = "uk",
    generation=0,
    image_url: str = "",
    media_fallback_reason: str = "",
    grid=None,
    needs_measurements: bool = False,
) -> CardPlan:
    """Розміри за §6: `0` / `1` / `2–3` / `4–13` / `>13`.

    Ніколи не показувати три з шести: набір кнопок або повний, або його немає
    зовсім. Клієнт, який побачив «S / M / L» замість шести, вирішить, що решти
    немає, і піде — і ми навіть не дізнаємось, чому.
    """
    lang = _lang(lang)
    available, disabled = readiness_sizes(readiness)
    product = (readiness or {}).get("product") or {}
    title = tpl.display_short(str(product.get("title") or ""))
    fit_label = _readiness_fit_label(readiness, lang)

    if not available:
        text = (
            _SIZE_NONE_WITH_DISABLED[lang].format(sizes=", ".join(disabled))
            if disabled
            else _SIZE_NONE[lang]
        )
        return CardPlan(
            kind=KIND_TEXT,
            payload=text,
            reason="no_available_sizes",
            lang=lang,
            generation=int(generation or 0),
            available_sizes=(),
            disabled_sizes=disabled,
            media_fallback_reason=str(media_fallback_reason or ""),
        )

    if len(available) == 1:
        # Один доступний розмір — це факт, а не вибір: кнопок немає зовсім (§6).
        return CardPlan(
            kind=KIND_TEXT,
            payload=_SIZE_ONE_ONLY[lang].format(size=available[0]),
            reason="single_available_size",
            lang=lang,
            generation=int(generation or 0),
            available_sizes=available,
            disabled_sizes=disabled,
            media_fallback_reason=str(media_fallback_reason or ""),
        )

    if needs_measurements or len(available) > tpl.MAX_QUICK_REPLIES:
        # Понад 13 значень фізично не влазять у quick replies, а урізаний набір
        # заборонений. Тому — сітка з замірами і повний текстовий перелік.
        return plan_size_grid_card(
            lang=lang,
            generation=generation,
            grid=grid,
            available_sizes=available,
            fit_label=fit_label,
            reason=(
                "measurements_requested" if needs_measurements else f"sizes:{len(available)}"
            ),
        )

    if len(available) <= tpl.MAX_BUTTONS_PER_ELEMENT:
        # 2–3 розміри влазять кнопками карточки, і карточка тут краща за текст:
        # разом з кнопками клієнт бачить фото саме того варіанта, який обирає.
        card = tpl.TemplateCard(
            title=title or _SIZE_CARD_TITLE[lang],
            subtitle=_SIZE_SUBTITLE[lang].format(sizes=", ".join(available)),
            image_url=str(image_url or ""),
            buttons=_size_buttons(available, lang=lang, generation=generation),
        )
        fallback = _SIZE_QUICK_TEXT[lang].format(sizes=", ".join(available))
        return CardPlan(
            kind=KIND_SINGLE_CARD,
            payload=tpl.GenericTemplate(cards=(card,), fallback_text=fallback),
            reason=f"sizes:{len(available)}",
            lang=lang,
            generation=int(generation or 0),
            available_sizes=available,
            disabled_sizes=disabled,
            media_fallback_reason=str(media_fallback_reason or ""),
        )

    # 4–13: усі доступні розміри quick replies. `Таблиця розмірів` додається
    # лише якщо після розмірів лишилось місце в лімітах провайдера — набір
    # розмірів урізати заради неї не можна.
    replies = [
        tpl.QuickReply(_size_label(size, lang), size_set_payload(generation, size))
        for size in available
    ]
    grid_reply_fits = len(replies) < tpl.MAX_QUICK_REPLIES
    if grid_reply_fits:
        replies.append(
            tpl.QuickReply(_label("grid_open", lang), size_grid_payload(generation))
        )
    text = _SIZE_QUICK_TEXT[lang].format(sizes=", ".join(available))
    if not grid_reply_fits:
        grid_url = str((grid or {}).get("image_url") or "")
        if grid_url:
            text = f"{text} {_label('grid_open', lang)}: {grid_url}"
    return CardPlan(
        kind=KIND_QUICK_REPLIES,
        payload=tpl.QuickReplyMessage(
            text=text, quick_replies=tuple(replies), fallback_text=text
        ),
        reason=f"sizes:{len(available)}",
        lang=lang,
        generation=int(generation or 0),
        available_sizes=available,
        disabled_sizes=disabled,
        media_fallback_reason=str(media_fallback_reason or ""),
    )


# --- Розмірна сітка ----------------------------------------------------------
# `NEW-SUBFUNNEL-001`, вузол `size_confidence`. Сила цієї карточки саме в тому,
# що вона переносить розмову з клієнта на товар: заміри — це факт про річ, а не
# питання про фігуру. Тому в копії нижче немає й не може бути жодної згадки про
# фігуру, вагу чи комплекцію, і числа беруться ТІЛЬКИ з реальної сітки.
FORBIDDEN_BODY_WORDS = (
    "фігур", "фигур", "комплекц", "вагу", "весу", "weight", "figure",
)

_GRID_TITLE = {
    "uk": "Розмірна сітка · {fit}",
    "ru": "Размерная сетка · {fit}",
    "en": "Size chart · {fit}",
}
_GRID_TITLE_NO_FIT = {
    "uk": "Розмірна сітка",
    "ru": "Размерная сетка",
    "en": "Size chart",
}
_GRID_SUBTITLE = {
    "uk": "Заміри: {columns}",
    "ru": "Замеры: {columns}",
    "en": "Measurements: {columns}",
}
_GRID_FALLBACK = {
    "uk": "Надсилаю заміри для крою {fit}. Доступні розміри: {sizes}.",
    "ru": "Отправляю замеры для кроя {fit}. Доступные размеры: {sizes}.",
    "en": "Here are the measurements for the {fit} cut. Available sizes: {sizes}.",
}
_GRID_FALLBACK_NO_FIT = {
    "uk": "Надсилаю заміри з таблиці. Доступні розміри: {sizes}.",
    "ru": "Отправляю замеры из таблицы. Доступные размеры: {sizes}.",
    "en": "Here are the measurements from the chart. Available sizes: {sizes}.",
}
_GRID_UNAVAILABLE = {
    "uk": "Таблиці замірів для цього крою в каталозі немає — уточню в менеджера, а вигадувати заміри не буду.",
    "ru": "Таблицы замеров для этого кроя в каталоге нет — уточню у менеджера, а придумывать замеры не стану.",
    "en": "The catalogue has no measurement chart for this cut — I will ask the manager rather than invent numbers.",
}


def plan_size_grid_card(
    *,
    lang: str = "uk",
    generation=0,
    grid=None,
    available_sizes=(),
    fit_label: str = "",
    reason: str = "",
) -> CardPlan:
    """Карточка сітки для КОНКРЕТНОГО крою: `Обрати розмір` і `Питання`.

    Крій обов'язковий саме тому, що оверсайз і класика мають різні заміри:
    «загальна» сітка тут не допомагає, а вводить в оману.
    """
    lang = _lang(lang)
    sizes = _sizes(available_sizes)
    image_url = str((grid or {}).get("image_url") or "")
    columns = tuple(
        str(value or "").strip()
        for value in (grid or {}).get("columns") or ()
        if str(value or "").strip()
    )
    buttons = (
        tpl.TemplateButton(
            tpl.BUTTON_POSTBACK,
            _label("grid_pick", lang),
            payload=size_reopen_payload(generation),
        ),
        tpl.TemplateButton(
            tpl.BUTTON_POSTBACK,
            _label("grid_question", lang),
            payload=size_question_payload(generation),
        ),
    )
    if not image_url and not columns:
        # Ні картинки, ні колонок — сітки просто немає. Карточка «Розмірна сітка»
        # без сітки гірша за честний текст.
        return CardPlan(
            kind=KIND_TEXT,
            payload=_GRID_UNAVAILABLE[lang],
            reason="grid_unavailable",
            lang=lang,
            generation=int(generation or 0),
            available_sizes=sizes,
        )
    title = (
        _GRID_TITLE[lang].format(fit=fit_label)
        if fit_label
        else _GRID_TITLE_NO_FIT[lang]
    )
    fallback_template = _GRID_FALLBACK[lang] if fit_label else _GRID_FALLBACK_NO_FIT[lang]
    fallback = fallback_template.format(fit=fit_label, sizes=", ".join(sizes) or "—")
    return CardPlan(
        kind=KIND_SIZE_GRID_CARD,
        payload=tpl.GenericTemplate(
            cards=(
                tpl.TemplateCard(
                    title=title,
                    subtitle=(
                        _GRID_SUBTITLE[lang].format(columns=", ".join(columns))
                        if columns
                        else ""
                    ),
                    image_url=image_url,
                    buttons=buttons,
                ),
            ),
            fallback_text=fallback,
        ),
        reason=reason or "size_grid",
        lang=lang,
        generation=int(generation or 0),
        available_sizes=sizes,
    )


def plan_size_visual(
    readiness,
    *,
    lang: str = "uk",
    generation=0,
    image_url: str = "",
    media_fallback_reason: str = "",
    grid=None,
    needs_measurements: bool = False,
) -> CardPlan | None:
    """Єдиний вхід для розміру. `None` = карточка вимкнена, іде текстовий шлях."""
    available, _disabled = readiness_sizes(readiness)
    wants_grid = bool(needs_measurements) or len(available) > tpl.MAX_QUICK_REPLIES
    if not card_enabled(FLAG_SIZE_GRID if wants_grid else FLAG_SIZE_CHOICE):
        return None
    return plan_size_choice(
        readiness,
        lang=lang,
        generation=generation,
        image_url=image_url,
        media_fallback_reason=media_fallback_reason,
        grid=grid,
        needs_measurements=needs_measurements,
    )


def plan_size_grid_visual(
    *,
    lang: str = "uk",
    generation=0,
    grid=None,
    available_sizes=(),
    fit_label: str = "",
) -> CardPlan | None:
    """Окремий вхід для натискання `Таблиця розмірів` — свій флаг."""
    if not card_enabled(FLAG_SIZE_GRID):
        return None
    return plan_size_grid_card(
        lang=lang,
        generation=generation,
        grid=grid,
        available_sizes=available_sizes,
        fit_label=fit_label,
        reason="grid_requested",
    )


# --- Читання реальних даних --------------------------------------------------


def card_generation(client) -> int:
    """`<gen>` для payload-ів: `candidate_generation` відкритої сесії вибору.

    Читання, без bootstrap: планування карточки не має права створювати
    комерційну сесію. Немає сесії — генерація нуль, і застаріле натискання
    однаково буде видно як розбіжність.
    """
    if not getattr(client, "pk", None):
        return 0
    try:
        from management.ig_bot_models import IgCommerceSelectionSession

        value = (
            IgCommerceSelectionSession.objects.filter(client_id=client.pk, open_slot=1)
            .order_by("-generation")
            .values_list("candidate_generation", flat=True)
            .first()
        )
        return int(value or 0)
    except Exception:
        return 0


def variant_image_url(
    product_id: int,
    *,
    color_variant_id=None,
    fit_code: str = "",
    size: str = "",
    selection_revision: str = "",
    expected_revision: str = "",
) -> tuple:
    """`(url, fallback_reason)` — фото РІВНО того варіанта (Э3.7 / `NEW-CAT-002`).

    Без цього кроку карточка стає видимою формою старої тихої проблеми: клієнт
    бачить фото чорного, натискає «Обрати» і отримує біле. `fallback_reason`
    їде в план і в лог, щоб оператор бачив причину generic-фото, а не гадав.
    """
    from management.services.ig_catalog_media import select_catalog_media

    try:
        selection = select_catalog_media(
            (product_id,),
            limit=1,
            color_variant_id=color_variant_id,
            fit_code=fit_code,
            size=size,
            selection_revision=selection_revision,
            expected_revision=expected_revision,
        )
    except Exception:
        return "", "media_lookup_failed"
    items = tuple(getattr(selection, "items", ()) or ())
    url = str(getattr(items[0], "url", "") or "") if items else ""
    return url, str(getattr(selection, "fallback_reason", "") or "")


def card_images(pairs) -> tuple:
    """`{product_id: url}` + перша причина fallback-у для сторінки каруселі.

    Запит на кожен товар окремо — не недогляд: `select_catalog_media()` враховує
    `color_variant_id` тільки коли просять один товар, тому «одним запитом на
    трьох» точні варіантні фото були б неможливі. Сторінка — три елементи, тобто
    три запити максимум.
    """
    images: dict = {}
    reason = ""
    for entry in tuple(pairs or ()):
        try:
            product_id, variant_id = entry
        except (TypeError, ValueError):
            product_id, variant_id = entry, None
        try:
            key = int(product_id)
        except (TypeError, ValueError):
            continue
        url, fallback_reason = variant_image_url(key, color_variant_id=variant_id)
        if url:
            images[key] = url
        if fallback_reason and not reason:
            reason = fallback_reason
    return images, reason


def size_grid_for_fit(product, fit_code: str, *, variant=None) -> dict:
    """Реальна сітка для конкретного крою: картинка, підписи колонок, розміри.

    Порожній результат — валідний факт («сітки немає»), а не помилка. Саме тому
    тут широкий `except`: відсутність сітки не повинна ламати хід, але й не має
    права перетворитись у вигадані заміри.
    """
    empty = {"image_url": "", "columns": (), "sizes": (), "resolved": False}
    if product is None:
        return empty
    code = str(fit_code or "").strip().lower()
    if not code:
        return empty
    try:
        from copy import deepcopy
        from management.services.ig_catalog_configured_choices import size_measurements

        from django.core.exceptions import ValidationError

        from product_catalog.size_grid_services import (
            normalize_size_grid_payload,
            resolve_effective_sizes,
            resolve_option_size_grid,
        )

        grid = resolve_option_size_grid(product, f"fit={code}", variant=variant)
        if grid is None:
            return empty
        from management.services.ig_catalog_media import _absolute_url

        image_url = ""
        image_field = getattr(grid, "image", None)
        if image_field:
            image_url = _absolute_url(getattr(image_field, "url", ""))
        columns: list = []
        try:
            guide = normalize_size_grid_payload(deepcopy(grid.guide_data or {}))
        except ValidationError:
            guide = {"columns": []}
        for column in guide.get("columns") or ():
            key = str((column or {}).get("key") or "")
            if key == "size":
                continue
            label = str((column or {}).get("label") or "").strip()
            if label and label not in columns:
                columns.append(label)
        sizes = tuple(
            str((row or {}).get("size") or "").strip().upper()
            for row in resolve_effective_sizes(product, f"fit={code}", variant=variant)
            if str((row or {}).get("size") or "").strip()
        )
        return {
            "image_url": image_url,
            "columns": tuple(columns),
            "sizes": sizes,
            "measurements": size_measurements(
                grid, product_id=product.pk, variant_id=getattr(variant, "pk", None),
                option_key=f"fit={code}", allowed_sizes=set(sizes)),
            "resolved": True,
        }
    except Exception:
        return empty


_SHOWN_ROW_PREFIX = {
    KIND_CAROUSEL: "карусель товарів",
    KIND_SINGLE_CARD: "карточка товару",
}


def record_shown_cards(client, sender_id: str, plan: CardPlan, delivery) -> list:
    """Зафіксувати показане так само, як це робить `record_shown_products`.

    Карусель замінює старий механізм «фото + список», але для решти системи
    нічого не змінюється: той самий ключ `sales_context`, той самий формат
    `position → product_id → title`, той самий службовий блок промпта
    (`shown_products_note`). Інакше «давай першу» після каруселі не мало б
    відповіді в принципі — рівно та поломка, через яку механізм і з'явився.

    Одна карусель — ОДИН рядок історії, а не три: у Meta це одне повідомлення з
    одним `message_id`, і три рядки з тим самим id читались би як три відправки.
    Позиції при цьому page-local: клієнт бачить три елементи й каже «перший» про
    перший з них, а не про четвертий у наскрізному списку.
    """
    import json

    from django.utils import timezone

    from management.models import InstagramBotMessage
    from management.services.ig_delivery_receipts import normalize_provider_message_id
    from management.services.instagram_bot import (
        CATALOG_MEDIA_SOURCE,
        SHOWN_PRODUCTS_CONTEXT_KEY,
        SHOWN_PRODUCTS_LIMIT,
        log,
    )

    shown = tuple(plan.shown or ())
    if not shown or not bool(getattr(delivery, "ok", False)):
        return []
    entries = [
        {
            "position": int(position),
            "product_id": int(product_id),
            "title": str(title or "")[:200],
        }
        for position, product_id, title in shown[:SHOWN_PRODUCTS_LIMIT]
    ]
    images = [
        str(getattr(card, "image_url", "") or "")
        for card in tuple(getattr(plan.payload, "cards", ()) or ())
        if str(getattr(card, "image_url", "") or "")
    ]
    prefix = _SHOWN_ROW_PREFIX.get(plan.kind, "карточка товару")
    titles = " | ".join(entry["title"] for entry in entries if entry["title"])
    try:
        InstagramBotMessage.objects.create(
            sender_id=sender_id,
            client=client,
            role=InstagramBotMessage.Role.MODEL,
            text=f"({prefix}: {titles})",
            status=InstagramBotMessage.Status.DONE,
            source=CATALOG_MEDIA_SOURCE,
            attachments=json.dumps(images, ensure_ascii=False) if images else "",
            provider_message_id=normalize_provider_message_id(
                getattr(delivery, "provider_message_id", "")
            ),
            processed_at=timezone.now(),
        )
    except Exception as exc:  # noqa: BLE001 - рядок історії не блокує хід
        log("warning", "shown_cards_row", repr(exc))

    if getattr(client, "pk", None):
        try:
            context = dict(getattr(client, "sales_context", {}) or {})
            context[SHOWN_PRODUCTS_CONTEXT_KEY] = {
                "at": timezone.now().isoformat(),
                "items": entries,
            }
            client.sales_context = context
            client.save(update_fields=["sales_context", "updated_at"])
        except Exception as exc:  # noqa: BLE001
            log("warning", "shown_cards_context", repr(exc))
    log(
        "info",
        "shown_cards",
        f"{sender_id}: {plan.kind} "
        + ", ".join(f"{entry['position']}={entry['product_id']}" for entry in entries),
    )
    return entries
