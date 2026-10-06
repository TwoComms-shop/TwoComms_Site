"""Маршрутизація інструкцій бота: за тригером поточного ходу, а не «завжди».

Прямий запит заказника: інструкції мають підключатись після певних сигналів або
тригерів, а не всі йти в контекст постійно.

Що було виміряно на проді перед цією правкою (щоб не переробляти двічі):

- інструкцій сім, усі активні, **жодної без тегів**, усі тексти дослівно рівні
  сіду — тобто адміністратор їх ще не правив;
- реальне покриття: **202 клієнти з 289 (70%) отримують рівно одну інструкцію
  з семи**, максимум по базі — чотири;
- блок інструкцій — 1037–1264 символи з промпта в 37 965–38 913, тобто ~3%;
- відбір ішов по зрізу чотирьох CRM-полів (`intent`, `stage`,
  `primary_objection`, `language`) і не знав ні тексту повідомлення, ні сигналів;
- половина явного маппінгу в `tags_for_client` була мертвим кодом: значення
  enum-полів і так додавались як теги, тому окремі гілки для `custom_print`,
  `payment_pending`, `prepayment`, `price`, `size` не робили нічого. Саме на цій
  оманливій таблиці згоріла правка W3: викинули `discount`, а інструкція
  прийшла через `price`;
- словник тегів ніде не описаний і не перевіряється, тому опечатка дає
  інструкцію, яка не спрацює ніколи. Гірше: правило «порожні теги = завжди»
  перетворює опечатку на протилежність задуму.

Три речі, які цей модуль додає.

**Тригер від поточного ходу.** Тег виду `on:size_question` спрацьовує лише тоді,
коли клієнт саме зараз спитав про розмір, а не тому що в картці з минулого тижня
лежить `objection=size`. Різниця видна на проді: у клієнта #5 стояв
`objection=size` при `intent=payment`, і розмірний playbook підмішувався в
повідомлення про оплату.

**Виключення.** Тег `not:paid` знімає інструкцію там, де вона шкодить. Раніше
єдине виключення (сервісне звернення) було захардкожене в Python, і будь-яке
нове правило вимагало правки коду замість розмітки.

**Валідація.** Невідомий тег більше не проходить молча: `unknown_tags` називає
його, а UI має показати це адміністратору. Опечатка — найдешевша й найчастіша
причина «інструкція є, але не працює».
"""
from __future__ import annotations

import re

# Префікси спеціальних тегів. Звичайний тег (без префікса) працює як раніше:
# збіг із будь-яким тегом клієнта.
TRIGGER_PREFIX = "on:"
EXCLUDE_PREFIX = "not:"

# Тригери поточного ходу. Ключ — назва тригера в розмітці, значення — що саме
# перевіряємо в тексті клієнта. Це навмисно вузький словник: тригер має бути
# однозначним, інакше він повторить помилку класифікатора, який матчив «скільки»
# як заперечення по ціні.
_TURN_TRIGGERS: dict[str, re.Pattern] = {
    # Питання про розмір/посадку саме зараз.
    "size_question": re.compile(
        r"(?:розмір|размер|size|сітк\w*|сетк\w*|міряти|мерить|обхват|зріст|рост|"
        r"вага|вес|підійде|подойдёт|подойдет|сяде|сядет)",
        re.IGNORECASE,
    ),
    # Пряме питання про ціну (не заперечення — саме питання).
    "price_question": re.compile(
        r"(?:скільки|сколько|ціна|цена|вартіст\w*|стоимост\w*|how much|price)",
        re.IGNORECASE,
    ),
    # Заперечення по ціні: дорого, не по кишені.
    "price_objection": re.compile(
        r"(?:дорого|дороговат\w*|задорого|не по кишен\w*|не по карман\w*|"
        r"дешевш\w*|дешевле|знижк\w*|скидк\w*|too expensive)",
        re.IGNORECASE,
    ),
    # Питання про доставку.
    "delivery_question": re.compile(
        r"(?:доставк\w*|нова пошта|новая почта|нп\b|відділенн\w*|отделени\w*|"
        r"поштомат|почтомат|коли прийде|когда придёт|когда придет|скільки їде|"
        r"сколько идёт|сколько идет|shipping|delivery)",
        re.IGNORECASE,
    ),
    # Питання про оплату.
    "payment_question": re.compile(
        r"(?:оплат\w*|сплатит\w*|заплатит\w*|передоплат\w*|предоплат\w*|"
        r"накладен\w*|наложк\w*|картк\w*|картой|монобанк|monobank|payment|pay)",
        re.IGNORECASE,
    ),
    # Сумнів, «подумаю».
    "hesitation": re.compile(
        r"(?:подумаю|поміркую|подумать|не знаю|вагаюсь|сомневаюсь|можливо пізніше|"
        r"может позже|потім напишу|потом напишу)",
        re.IGNORECASE,
    ),
    # Кастомний принт.
    "custom_print": re.compile(
        r"(?:свій принт|свой принт|власн\w* принт|кастом\w*|намалю\w*|"
        r"нанест\w*|свій дизайн|свой дизайн|логотип компан\w*)",
        re.IGNORECASE,
    ),
}

TURN_TRIGGER_NAMES = frozenset(_TURN_TRIGGERS)


def turn_triggers(text: str) -> set[str]:
    """Які тригери спрацювали на цьому повідомленні клієнта.

    Порожній текст дає порожню множину, і це важливо: інструкція з тригером не
    має підмішуватись у хід, де клієнт нічого не написав (наприклад, надіслав
    лише фото). Раніше такої різниці не існувало взагалі.
    """
    value = str(text or "")
    if not value.strip():
        return set()
    # Objections use the same customer's-own-current-clause predicate as the
    # captured route. Lexical mentions in quotes, history or a negation do not
    # admit a sales objection instruction. Other question selectors retain
    # their established matching contract.
    from management.services.ig_turn_intelligence import current_objections

    kinds = {item["kind"] for item in current_objections([{"message_id": 0, "text": value}])}
    result = {name for name, pattern in _TURN_TRIGGERS.items()
              if name not in {"price_objection", "hesitation"} and pattern.search(value)}
    if kinds & {"price", "cheaper_elsewhere"}:
        result.add("price_objection")
    if "thinking" in kinds:
        result.add("hesitation")
    return result


def split_instruction_tags(raw: str) -> dict:
    """Розібрати розмітку інструкції на звичайні теги, тригери й виключення."""
    plain: set[str] = set()
    triggers: set[str] = set()
    excludes: set[str] = set()
    for chunk in str(raw or "").replace(";", ",").split(","):
        tag = chunk.strip().lower()
        if not tag:
            continue
        if tag.startswith(TRIGGER_PREFIX):
            name = tag[len(TRIGGER_PREFIX):].strip()
            if name:
                triggers.add(name)
        elif tag.startswith(EXCLUDE_PREFIX):
            name = tag[len(EXCLUDE_PREFIX):].strip()
            if name:
                excludes.add(name)
        else:
            plain.add(tag)
    return {"plain": plain, "triggers": triggers, "excludes": excludes}


def known_tags() -> set[str]:
    """Повний словник допустимих звичайних тегів.

    Складається з фактичних значень enum'ів (саме вони й потрапляють у теги
    клієнта) плюс службові. Раніше цього переліку не існувало ніде, тому
    опечатка в теге не мала жодного способу проявитись.
    """
    # Службові теги, яких немає серед значень enum'ів. `stop` і `no_buy` уже
    # використовує прод-інструкція «Stop / No-buy» — словник має її приймати,
    # інакше валідація почне лаятись на робочу розмітку.
    tags = {"global", "core", "sales", "post_sale", "service", "product", "catalog",
            "payment", "payment_pending", "discount", "fit", "exchange", "return",
            "stop", "no_buy", "opt_out"}
    try:
        from management.models import IgClient, IgObjection

        for enum in (IgClient.Stage, IgClient.Intent, IgClient.Objection):
            tags.update(str(item.value).lower() for item in enum)
        tags.update(f"objection_{item.value}" for item in IgObjection.Type)
    except Exception:  # noqa: BLE001 - валідація не має ламати рендер
        pass
    tags.update({"uk", "ru", "en"})
    return tags


def validate_instruction_tags(raw: str) -> dict:
    """Перевірити розмітку. Повертає `{unknown_tags, unknown_triggers}`.

    Свідомо не забороняє збереження: адміністратор має бачити попередження, але
    не втрачати текст, який щойно набрав (F-UX-006 — таб «Інструкції» вже
    втрачав поля при помилці).
    """
    parts = split_instruction_tags(raw)
    allowed = known_tags()
    return {
        "unknown_tags": sorted(tag for tag in parts["plain"] if tag not in allowed),
        "unknown_triggers": sorted(
            name for name in parts["triggers"] if name not in TURN_TRIGGER_NAMES
        ),
    }


def instruction_matches(
    raw_tags: str,
    client_tags: set[str],
    *,
    active_triggers: set[str] | None = None,
) -> bool:
    """Чи підходить інструкція цьому клієнту на цьому ході.

    Порядок перевірок від найсильнішого до найслабшого:
    1. виключення (`not:*`) — вето, навіть якщо решта збігається;
    2. тригери (`on:*`) — якщо оголошені, потрібен хоча б один спрацьований;
    3. звичайні теги — збіг хоча б по одному, як було раніше;
    4. немає жодного тега — інструкція глобальна.
    """
    parts = split_instruction_tags(raw_tags)
    client_tags = client_tags or set()
    active_triggers = active_triggers or set()

    if parts["excludes"] & client_tags:
        return False
    if parts["triggers"] and not (parts["triggers"] & active_triggers):
        return False
    if parts["plain"]:
        return bool(parts["plain"] & client_tags)
    # Тег лише тригерний: спрацював тригер — інструкція підходить.
    return True if parts["triggers"] else True
