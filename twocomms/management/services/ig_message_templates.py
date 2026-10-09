"""Транспорт інтерактивних карточок Instagram і валідатор лімітів Meta.

До цього модуля всі вихідні повідомлення були чистим текстом
(`{"message": {"text": part}}`), а єдиним вкладенням — картинка каталогу.
Можливості надіслати карточку з кнопками в проєкті не існувало як такої.

Перевірено по документації Meta 2026-08-28
(`developers.facebook.com/docs/messenger-platform/instagram/features/generic-template/`,
`.../quick-replies/`):

* `template_type=generic` доступний для Instagram;
* кнопки — **тільки** `web_url` і `postback`, максимум 3 на елемент;
* до 10 елементів на повідомлення (горизонтальна карусель);
* `title` ≤ 80 символів, `subtitle` ≤ 80 символів;
* елемент обов'язково має мати хоч одне поле крім `title`;
* `quick_replies` — до 13, ≤ 20 символів на кнопку;
* шаблон **не рендериться** у web-версії Instagram.

Останній пункт — головне обмеження дизайну: карточка ніколи не може бути
єдиним носієм смислу. Тому кожна карточка тут створюється РАЗОМ з двома
текстами: `fallback_text` (те, що отримає клієнт, якщо провайдер відхилить
карточку) і `projection_text` (те, що побачить модель і оператор в історії —
модель не «бачить» кнопки, і порожній рядок в історії втратив би контекст ходу).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace

# Ліміти Meta. Змінювати тільки разом з посиланням на документацію.
MAX_ELEMENTS = 10
MAX_BUTTONS_PER_ELEMENT = 3
MAX_TITLE_CHARS = 80
MAX_SUBTITLE_CHARS = 80
MAX_BUTTON_TITLE_CHARS = 20
MAX_POSTBACK_PAYLOAD_CHARS = 1000
MAX_QUICK_REPLIES = 13
MAX_QUICK_REPLY_TITLE_CHARS = 20
MAX_TEXT_BYTES = 1000
MAX_BUTTON_TEMPLATE_TEXT_CHARS = 640

# Фіксований словник підписів кнопок з гарантованою довжиною ≤20 символів.
# Автоматичні підписи беруться ТІЛЬКИ звідси; це запобігає випадковому
# перевищенню ліміту через зміну фразування в промпті чи налаштуваннях
# (NEW-TMPL-002: обрізка з'їдала номер відділення — найважливіше поле).
BUTTON_LABELS = {
    # --- Вибір товару: єдиний випадок, де кнопки реально знімають крок ---
    # Розміри йдуть quick replies, а не кнопками карточки: Meta дозволяє до 13
    # quick replies і лише 3 кнопки на елемент, а розмірна сітка класики
    # доходить до шести значень (XS…2XL). Показуються ТІЛЬКИ ті розміри, які є в
    # наявності для конкретного варіанта: кнопка на відсутній розмір — це не
    # зручність, а обіцянка, якої ми не виконаємо.
    "size_xs": {"uk": "XS", "ru": "XS", "en": "XS"},
    "size_s": {"uk": "S", "ru": "S", "en": "S"},
    "size_m": {"uk": "M", "ru": "M", "en": "M"},
    "size_l": {"uk": "L", "ru": "L", "en": "L"},
    "size_xl": {"uk": "XL", "ru": "XL", "en": "XL"},
    # Нотація саме така, як у каталозі: XXL/XXXL, а не 2XL/3XL. Змішувати їх
    # («XL / 2XL / XXXL») виглядає як недогляд, а клієнт бачить розміри на сайті
    # у нотації каталогу — вона й лишається джерелом істини.
    "size_xxl": {"uk": "XXL", "ru": "XXL", "en": "XXL"},
    "size_xxxl": {"uk": "XXXL", "ru": "XXXL", "en": "XXXL"},

    # Колір — теж одне вимірювання за хід, і теж лише sellable-варіанти.
    "color_black": {"uk": "Чорне", "ru": "Чёрное", "en": "Black"},
    "color_white": {"uk": "Біле", "ru": "Белое", "en": "White"},

    # --- Карусель товарів і категорій ---
    # `select` дає боту однозначний товар замість «давай другу фотографію».
    "product_select": {"uk": "Обрати", "ru": "Выбрать", "en": "Choose"},
    "product_details": {"uk": "Детальніше", "ru": "Подробнее", "en": "Details"},
    "catalog_more": {"uk": "Показати ще", "ru": "Показать ещё", "en": "Show more"},
    "size_help": {"uk": "Допоможіть обрати", "ru": "Помогите выбрать", "en": "Help me choose"},

    # --- Розмірна сітка ---
    # Ключі свідомо НЕ починаються з `size_`: цей префікс зарезервований під
    # значення розмірів, і тест перевіряє, що їх рівно сім. Сітка — це не восьмий
    # розмір, а інша дія.
    #
    # `grid_open` веде до замірів для конкретного крою; `grid_pick` повертає до
    # вибору розміру після перегляду; `grid_question` відкриває розмову про
    # замір. Кнопки «Написати менеджеру» тут немає і бути не може: клієнт уже
    # пише, а питання про замір — це питання про товар, не запит на handoff.
    "grid_open": {"uk": "Таблиця розмірів", "ru": "Таблица размеров", "en": "Size chart"},
    "grid_pick": {"uk": "Обрати розмір", "ru": "Выбрать размер", "en": "Choose size"},
    "grid_question": {"uk": "Питання", "ru": "Вопрос", "en": "Ask a question"},

    # --- Оформлення й оплата ---
    # Кнопка замінює довге посилання: клієнт бачить дію, а не URL.
    "pay_online": {"uk": "Сплатити онлайн", "ru": "Оплатить онлайн", "en": "Pay online"},
    # Посилання на магазин теж мусить бути кнопкою: голий URL у Instagram
    # виглядає як спам і не має підпису дії.
    "open_site": {"uk": "Відкрити магазин", "ru": "Открыть магазин", "en": "Open shop"},
    "order_continue": {"uk": "Продовжити", "ru": "Продолжить", "en": "Continue"},
    "order_change": {"uk": "Змінити", "ru": "Изменить", "en": "Change"},
    "payment_check": {"uk": "Перевірити оплату", "ru": "Проверить оплату", "en": "Check payment"},

    # --- Маркетинговий opt-in: одна кнопка, БЕЗ кнопки відмови ---
    # Кнопки «Ні, дякую» тут немає свідомо: відмова — це просто відсутність
    # натискання, а друга кнопка лише знижує конверсію і нічого не додає.
    # Формулювання називає вигоду (ТТН + знижки), а не дозвіл.
    "consent_yes": {
        "uk": "Так, отримувати",
        "ru": "Да, получать",
        "en": "Yes, keep me posted",
    },

    # --- Доставка ---
    # `Забрав` / `Ще не отримав` тут НЕМАЄ і бути не може: факт отримання дає
    # API Нової Пошти, а не клієнт. Питати про те, що ми вже знаємо, — це і
    # зайвий крок, і сигнал, що з клієнтом говорить автомат.
    "track_parcel": {
        "uk": "Відстежити",
        "ru": "Отследить",
        "en": "Track",
    },
    "remind_later": {
        "uk": "Нагадати пізніше",
        "ru": "Напомнить позже",
        "en": "Remind me later",
    },
}

def button_label(key: str, lang: str = "uk") -> str:
    """Отримати підпис кнопки з багатомовного словника.
    
    Args:
        key: Ключ кнопки (наприклад, "pay_online")
        lang: Мова ("uk", "ru", "en"), за замовчуванням "uk"
    
    Returns:
        Підпис кнопки або порожній рядок
    
    Примітка про те, чого тут НЕ буде:
        * підтвердження отримання посилки — факт дає API Нової Пошти;
        * «Написати менеджеру» — клієнт уже пише;
        * «Скасувати» / «Ні, дякую» — відмова це відсутність натискання;
        * «оверсайз / не оверсайз» — усі худі зараз злегка оверсайзні, вибору немає.

    Повний контракт «яка кнопка в який момент і коли зникає» —
    `docs/instagram_bot_audit/new/10_VISUAL_MESSAGING.md`.
    """
    labels = BUTTON_LABELS.get(str(key or "").strip(), {})
    if isinstance(labels, dict):
        return labels.get(lang, labels.get("uk", ""))
    return ""


MAX_DISPLAY_SHORT_CHARS = 40
# Нижче цієї межі назва перестає бути описовою: «Vortex» не каже, худі це чи
# футболка, і карточка з таким заголовком гірша за довшу.
MIN_DESCRIPTIVE_CHARS = 12


def display_short(full_name: str, manual: str = "") -> str:
    """Коротке ім'я товару для карточки — без вигляду обрубка.

    Автообрізка по ліміту давала «Худі Vortex Tactical Editi…»: клієнт читає це
    як баг інтерфейсу, а не як скорочення. Тому три правила по спаданню
    надійності, і еліпсис — останній засіб, а не перший.

    1. Задане вручну коротке ім'я, якщо воно влазить. Людина знає бренд краще за
       правило.
    2. Кома як **семантична** межа. У назвах виду «Худі Premium, оверсайз,
       чорне» після коми починаються атрибути, а не назва, тому ліва частина —
       це і є ім'я товару. Дефіс такою межею НЕ вважається: у «Vortex -
       Tactical Edition Оверсайз» ліва частина втрачає тип товару, і карточка з
       заголовком «Vortex» стає неоднозначною.
    3. Обрізка по межі слова **без** еліпсиса. «Худі Vortex Tactical Edition
       Оверсайз» читається як повне ім'я, а «Худі Vortex Tactical Editi…» — як
       поломка.

    Еліпсис лишається тільки для випадку, коли навіть перше слово довше за
    ліміт: там будь-який варіант поганий, і краще явно показати, що текст
    урізаний.
    """
    manual_clean = _clean(manual)
    if manual_clean and len(manual_clean) <= MAX_DISPLAY_SHORT_CHARS:
        return manual_clean

    full_clean = _clean(full_name)
    if len(full_clean) <= MAX_DISPLAY_SHORT_CHARS:
        return full_clean

    # Правило 2: кома відділяє атрибути від імені.
    if ", " in full_clean:
        head = full_clean.split(", ", 1)[0].strip()
        if MIN_DESCRIPTIVE_CHARS <= len(head) <= MAX_DISPLAY_SHORT_CHARS:
            return head

    # Правило 3: стільки перших слів, скільки влазить, без еліпсиса.
    words = full_clean.split(" ")
    kept: list = []
    for word in words:
        candidate = " ".join(kept + [word])
        if len(candidate) > MAX_DISPLAY_SHORT_CHARS:
            break
        kept.append(word)
    trimmed = " ".join(kept)
    if len(trimmed) >= MIN_DESCRIPTIVE_CHARS:
        return trimmed

    # Останній засіб: одне слово довше за ліміт.
    return _truncate(full_clean, MAX_DISPLAY_SHORT_CHARS)


BUTTON_POSTBACK = "postback"
BUTTON_WEB_URL = "web_url"

# Версія схеми payload-ів кнопок. Формат: `<ns>:<version>:<action>[:<arg>...]`.
# Версія обов'язкова: без неї натискання на старій карточці після зміни семантики
# виконало б не ту дію, яку клієнт бачив на екрані.
PAYLOAD_VERSION = "1"
PAYLOAD_NAMESPACE = "twc"

_WHITESPACE_RE = re.compile(r"\s+")


class TemplateValidationError(ValueError):
    """Карточка не може бути приведена до лімітів Meta без втрати смислу."""


@dataclass(frozen=True)
class TemplateButton:
    kind: str
    title: str
    payload: str = ""
    url: str = ""


@dataclass(frozen=True)
class TemplateCard:
    title: str
    subtitle: str = ""
    image_url: str = ""
    default_url: str = ""
    buttons: tuple = ()


@dataclass(frozen=True)
class GenericTemplate:
    """Карточка разом з обома текстами, а не замість них."""

    cards: tuple
    fallback_text: str
    projection_text: str = ""
    quick_replies: tuple = ()
    # Стабільний ключ карточки для звірки з входящим postback.
    correlation_key: str = ""
    degraded_fields: tuple = field(default=())


@dataclass(frozen=True)
class ButtonTemplate:
    """Compact text card with 1–3 native Meta buttons."""

    text: str
    buttons: tuple
    fallback_text: str = ""
    projection_text: str = ""
    degraded_fields: tuple = field(default=())


@dataclass(frozen=True)
class QuickReply:
    title: str
    payload: str


@dataclass(frozen=True)
class QuickReplyMessage:
    """Текст із швидкими відповідями — саме та форма, яку Meta для них має.

    Це не дублікат `GenericTemplate.quick_replies`. Quick replies у Meta живуть
    на ЗВИЧАЙНОМУ текстовому повідомленні; на generic-шаблоні вони теж
    допускаються, але тоді шаблон мусить мати валідний елемент. Спроба показати
    «шість кнопок розміру» карточкою з одним лише title давала рівно такий
    результат: валідатор (правильно) відкидав елемент без другого поля,
    `send_template` падав у текстовий fallback — і клієнт отримував текст БЕЗ
    жодної кнопки, а результат відправки при цьому читався як `ok=True`.
    Мовчазна деградація найгіршого роду: питання поставлене, відповісти нічим.
    """

    text: str
    quick_replies: tuple
    fallback_text: str = ""
    projection_text: str = ""
    degraded_fields: tuple = field(default=())


def build_payload(action: str, *args: str) -> str:
    """Зібрати версіонований payload кнопки.

    Наявна в проєкті конвенція `commerce:<generation>:select:<n>`
    (`ig_commerce_state`) залишається дійсною; новий простір імен `twc:1:*` не
    конфліктує з нею і не потребує її зміни.
    """
    parts = [PAYLOAD_NAMESPACE, PAYLOAD_VERSION, str(action or "")]
    parts.extend(str(value or "") for value in args)
    payload = ":".join(parts)
    if len(payload) > MAX_POSTBACK_PAYLOAD_CHARS:
        raise TemplateValidationError("postback payload too long")
    return payload


def parse_payload(raw: str) -> dict:
    """Розібрати payload кнопки. Невідомий формат — це не помилка, а None-результат."""
    text = str(raw or "").strip()
    if not text:
        return {}
    parts = text.split(":")
    if len(parts) < 3 or parts[0] != PAYLOAD_NAMESPACE:
        return {}
    return {
        "namespace": parts[0],
        "version": parts[1],
        "action": parts[2],
        "args": tuple(parts[3:]),
        "raw": text,
    }


def _clean(value: str) -> str:
    return _WHITESPACE_RE.sub(" ", str(value or "")).strip()


def _clean_paragraphs(value: str) -> str:
    """Keep intentional message paragraphs; button labels stay single-line."""
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [_clean(line) for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _truncate(value: str, limit: int) -> str:
    """Обрізати по межі слова, а не посередині — інакше клієнт бачить обрубок."""
    clean = _clean(value)
    if len(clean) <= limit:
        return clean
    head = clean[: max(1, limit - 1)]
    if " " in head:
        head = head.rsplit(" ", 1)[0]
    return f"{head.rstrip()}…"


_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})


def _trusted_image_url(url: str) -> bool:
    """Картинка карточки — тільки first-party HTTPS-ассет.

    Свідомо не використовує `ig_catalog_media._trusted_media_item`: та перевірка
    додатково вимагає mime-тип і розмір у байтах, бо там ми самі віддаємо файл.
    Тут картинку завантажує Meta по URL, тому відомі лише схема, хост і
    розширення. Недовірений URL не робить карточку помилкою — поле просто
    знімається (деградація полів, а не відмова).
    """
    from pathlib import Path
    from urllib.parse import urlsplit

    from django.conf import settings as django_settings

    try:
        parsed = urlsplit(str(url or ""))
        if parsed.scheme != "https" or not parsed.hostname:
            return False
        from management.services.ig_catalog_media import _base_url

        base_host = urlsplit(_base_url()).hostname
        configured = getattr(django_settings, "IG_CATALOG_MEDIA_ALLOWED_HOSTS", ()) or ()
        allowed = {str(base_host or "").lower()}
        allowed.update(
            str(value).strip().lower() for value in configured if str(value).strip()
        )
        if parsed.hostname.lower() not in allowed:
            return False
        return (Path(parsed.path).suffix or "").lower() in _IMAGE_SUFFIXES
    except (TypeError, ValueError):
        return False


def normalize_template(template: GenericTemplate) -> GenericTemplate:
    """Привести карточку до лімітів Meta деградацією полів, а не відмовою.

    Порядок деградації від найменш до найбільш болючого: обрізання тексту →
    зняття підзаголовка → зняття недовіреної картинки → відкидання зайвих кнопок
    → відкидання зайвих елементів. Відмова (виключення) залишається тільки для
    випадку, коли не лишається жодного валідного елемента: тоді викликаючий шар
    відправляє текстовий еквівалент.
    """
    degraded: list = []
    cards: list = []
    for card in template.cards[:MAX_ELEMENTS]:
        title = _truncate(card.title, MAX_TITLE_CHARS)
        if not title:
            degraded.append("element_without_title")
            continue
        if len(_clean(card.title)) > MAX_TITLE_CHARS:
            degraded.append("title_truncated")
        subtitle = _truncate(card.subtitle, MAX_SUBTITLE_CHARS)
        if card.subtitle and len(_clean(card.subtitle)) > MAX_SUBTITLE_CHARS:
            degraded.append("subtitle_truncated")
        image_url = str(card.image_url or "")
        if image_url and not _trusted_image_url(image_url):
            degraded.append("image_dropped_untrusted")
            image_url = ""
        buttons: list = []
        for button in card.buttons:
            if len(buttons) >= MAX_BUTTONS_PER_ELEMENT:
                degraded.append("buttons_truncated")
                break
            button_title = _truncate(button.title, MAX_BUTTON_TITLE_CHARS)
            if not button_title:
                degraded.append("button_without_title")
                continue
            if button.kind == BUTTON_POSTBACK:
                payload = str(button.payload or "").strip()
                if not payload or len(payload) > MAX_POSTBACK_PAYLOAD_CHARS:
                    degraded.append("button_payload_invalid")
                    continue
                buttons.append(TemplateButton(BUTTON_POSTBACK, button_title, payload=payload))
            elif button.kind == BUTTON_WEB_URL:
                url = str(button.url or "").strip()
                if not url.startswith("https://"):
                    degraded.append("button_url_invalid")
                    continue
                buttons.append(TemplateButton(BUTTON_WEB_URL, button_title, url=url))
            else:
                # Meta для Instagram підтримує тільки ці два типи.
                degraded.append("button_kind_unsupported")
        default_url = str(card.default_url or "").strip()
        if default_url and not default_url.startswith("https://"):
            degraded.append("default_action_dropped")
            default_url = ""
        # Елемент валідний тільки якщо має хоч одне поле крім title.
        if not (subtitle or image_url or buttons or default_url):
            degraded.append("element_without_secondary_field")
            continue
        cards.append(
            TemplateCard(
                title=title,
                subtitle=subtitle,
                image_url=image_url,
                default_url=default_url,
                buttons=tuple(buttons),
            )
        )
    if len(template.cards) > MAX_ELEMENTS:
        degraded.append("elements_truncated")
    if not cards:
        raise TemplateValidationError("no valid template element remains")

    quick_replies: list = []
    for reply in template.quick_replies[:MAX_QUICK_REPLIES]:
        title = _truncate(reply.title, MAX_QUICK_REPLY_TITLE_CHARS)
        payload = str(reply.payload or "").strip()
        if not title or not payload:
            degraded.append("quick_reply_invalid")
            continue
        quick_replies.append(QuickReply(title, payload))
    if len(template.quick_replies) > MAX_QUICK_REPLIES:
        degraded.append("quick_replies_truncated")

    fallback = _clean(template.fallback_text)
    if not fallback:
        raise TemplateValidationError("template requires a text fallback")
    if len(fallback.encode("utf-8")) > MAX_TEXT_BYTES:
        raise TemplateValidationError("fallback text exceeds the provider limit")

    return replace(
        template,
        cards=tuple(cards),
        quick_replies=tuple(quick_replies),
        fallback_text=fallback,
        projection_text=template.projection_text or build_projection(tuple(cards)),
        degraded_fields=tuple(dict.fromkeys(degraded)),
    )


def normalize_button_template(template: ButtonTemplate) -> ButtonTemplate:
    """Normalize Meta's compact button template without hiding lost actions."""
    degraded: list[str] = []
    source_text = _clean(template.text)
    if not source_text:
        raise TemplateValidationError("button template requires text")
    text = _truncate(source_text, MAX_BUTTON_TEMPLATE_TEXT_CHARS)
    if len(source_text) > MAX_BUTTON_TEMPLATE_TEXT_CHARS:
        degraded.append("button_template_text_truncated")

    buttons: list[TemplateButton] = []
    for button in tuple(template.buttons or ()):
        if len(buttons) >= MAX_BUTTONS_PER_ELEMENT:
            degraded.append("buttons_truncated")
            break
        title = _truncate(button.title, MAX_BUTTON_TITLE_CHARS)
        if not title:
            degraded.append("button_without_title")
            continue
        if button.kind == BUTTON_POSTBACK:
            payload = str(button.payload or "").strip()
            if not payload or len(payload) > MAX_POSTBACK_PAYLOAD_CHARS:
                degraded.append("button_payload_invalid")
                continue
            buttons.append(TemplateButton(BUTTON_POSTBACK, title, payload=payload))
        elif button.kind == BUTTON_WEB_URL:
            url = str(button.url or "").strip()
            if not url.startswith("https://"):
                degraded.append("button_url_invalid")
                continue
            buttons.append(TemplateButton(BUTTON_WEB_URL, title, url=url))
        else:
            degraded.append("button_kind_unsupported")
    if not buttons:
        raise TemplateValidationError("button template requires a valid button")

    fallback = _clean(template.fallback_text or text)
    if len(fallback.encode("utf-8")) > MAX_TEXT_BYTES:
        raise TemplateValidationError("fallback text exceeds the provider limit")
    projection = template.projection_text or (
        f"(надіслано кнопковий шаблон: {text}; кнопки: "
        f"{' / '.join(button.title for button in buttons)})"
    )
    return replace(
        template,
        text=text,
        buttons=tuple(buttons),
        fallback_text=fallback,
        projection_text=projection,
        degraded_fields=tuple(dict.fromkeys(degraded)),
    )


def normalize_quick_reply_message(message: QuickReplyMessage) -> QuickReplyMessage:
    """Привести текст із швидкими відповідями до лімітів, не ховаючи втрат.

    Порожній набір валідних кнопок — це помилка, а не «просто текст»: у такому
    разі краще явно впасти у текстовий fallback вище, ніж надіслати питання, на
    яке нічим відповісти, і повідомити про успіх.
    """
    degraded: list[str] = []
    text = _clean_paragraphs(message.text)
    if not text:
        raise TemplateValidationError("quick reply message requires text")
    if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        raise TemplateValidationError("quick reply text exceeds the provider limit")

    replies: list[QuickReply] = []
    for reply in tuple(message.quick_replies or ())[:MAX_QUICK_REPLIES]:
        title = _truncate(reply.title, MAX_QUICK_REPLY_TITLE_CHARS)
        payload = str(reply.payload or "").strip()
        if not title or not payload:
            degraded.append("quick_reply_invalid")
            continue
        if len(payload) > MAX_POSTBACK_PAYLOAD_CHARS:
            degraded.append("quick_reply_payload_too_long")
            continue
        replies.append(QuickReply(title, payload))
    if len(tuple(message.quick_replies or ())) > MAX_QUICK_REPLIES:
        degraded.append("quick_replies_truncated")
    if not replies:
        raise TemplateValidationError("quick reply message requires a valid reply")

    fallback = _clean_paragraphs(message.fallback_text or text)
    if len(fallback.encode("utf-8")) > MAX_TEXT_BYTES:
        raise TemplateValidationError("fallback text exceeds the provider limit")
    projection = message.projection_text or (
        f"(надіслано швидкі відповіді: {text}; варіанти: "
        f"{' / '.join(reply.title for reply in replies)})"
    )
    return replace(
        message,
        text=text,
        quick_replies=tuple(replies),
        fallback_text=fallback,
        projection_text=projection,
        degraded_fields=tuple(dict.fromkeys(degraded)),
    )


def build_projection(cards: tuple) -> str:
    """Текстова проєкція карточки для історії моделі й для оператора.

    Модель не «бачить» кнопки. Якщо в історію попаде порожній рядок, наступний
    хід втратить контекст і бот перепитає те саме. Формат читається і людиною:
    `(надіслано карточку: Худі Vortex — чорне; кнопки: S / M / L)`.
    """
    parts = []
    for card in cards:
        titles = " / ".join(button.title for button in card.buttons)
        chunk = card.title
        if card.subtitle:
            chunk = f"{chunk} — {card.subtitle}"
        if titles:
            chunk = f"{chunk}; кнопки: {titles}"
        parts.append(chunk)
    return f"(надіслано карточку: {' | '.join(parts)})"


def template_message_payload(template: GenericTemplate) -> dict:
    message = {
        "attachment": {
            "type": "template",
            "payload": {
                "template_type": "generic",
                "elements": [_element_payload(card) for card in template.cards],
            },
        }
    }
    if template.quick_replies:
        message["quick_replies"] = [
            {
                "content_type": "text",
                "title": reply.title,
                "payload": reply.payload,
            }
            for reply in template.quick_replies
        ]
    return message


def button_template_message_payload(template: ButtonTemplate) -> dict:
    return {
        "attachment": {
            "type": "template",
            "payload": {
                "template_type": "button",
                "text": template.text,
                "buttons": [
                    (
                        {
                            "type": BUTTON_POSTBACK,
                            "title": button.title,
                            "payload": button.payload,
                        }
                        if button.kind == BUTTON_POSTBACK
                        else {
                            "type": BUTTON_WEB_URL,
                            "title": button.title,
                            "url": button.url,
                        }
                    )
                    for button in template.buttons
                ],
            },
        }
    }


def quick_reply_message_payload(message: QuickReplyMessage) -> dict:
    """`{"text": ..., "quick_replies": [...]}` — без вкладення attachment."""
    return {
        "text": message.text,
        "quick_replies": [
            {
                "content_type": "text",
                "title": reply.title,
                "payload": reply.payload,
            }
            for reply in message.quick_replies
        ],
    }


def _element_payload(card: TemplateCard) -> dict:
    element = {"title": card.title}
    if card.subtitle:
        element["subtitle"] = card.subtitle
    if card.image_url:
        element["image_url"] = card.image_url
    if card.default_url:
        element["default_action"] = {"type": "web_url", "url": card.default_url}
    if card.buttons:
        element["buttons"] = [
            (
                {"type": BUTTON_POSTBACK, "title": button.title, "payload": button.payload}
                if button.kind == BUTTON_POSTBACK
                else {"type": BUTTON_WEB_URL, "title": button.title, "url": button.url}
            )
            for button in card.buttons
        ]
    return element


@dataclass(frozen=True)
class TemplateDelivery:
    """Результат відправки з тими ж гарантіями, що й у текстового шляху."""

    ok: bool
    kind: str = "unknown"
    hint: str = ""
    provider_message_id: str = ""
    used_text_fallback: bool = False
    degraded_fields: tuple = field(default=())
    projection_text: str = ""

    def __bool__(self) -> bool:  # pragma: no cover - trivial
        return self.ok


@dataclass(frozen=True)
class PreparedTemplateEffects:
    """Exact dormant-outbox effect specs; this object performs no send."""

    effects: tuple[dict, ...] = ()
    degraded_fields: tuple = field(default=())
    projection_text: str = ""
    error: str = ""


def prepare_template_effects(
    recipient_id: str,
    template: GenericTemplate,
    *,
    allow_text_fallback: bool = True,
    rejection_code: str = "provider_rejected",
) -> PreparedTemplateEffects:
    """Prepare primary and conditional fallback payloads without provider I/O."""
    recipient = str(recipient_id or "").strip()
    if not recipient:
        return PreparedTemplateEffects(error="recipient_missing")
    try:
        normalized = normalize_template(template)
    except TemplateValidationError as exc:
        fallback = _clean(template.fallback_text)
        if allow_text_fallback and fallback:
            return PreparedTemplateEffects(effects=({
                "group": "substantive_text",
                "kind": "text",
                "payload": {
                    "recipient": {"id": recipient},
                    "message": {"text": fallback},
                },
            },), error=str(exc))
        return PreparedTemplateEffects(error=str(exc))
    effects = [{
        "group": "template",
        "kind": "template",
        "payload": {
            "recipient": {"id": recipient},
            "message": template_message_payload(normalized),
        },
    }]
    fallback = _clean(normalized.fallback_text)
    if allow_text_fallback and fallback:
        effects.append({
            "group": "template_fallback",
            "kind": "fallback",
            "payload": {
                "recipient": {"id": recipient},
                "message": {"text": fallback},
            },
            "activation": {
                "group": "template",
                "part_index": 0,
                "failure_code": (
                    rejection_code
                    if rejection_code in {"provider_rejected", "link_rejected"}
                    else "provider_rejected"
                ),
            },
        })
    return PreparedTemplateEffects(
        effects=tuple(effects),
        degraded_fields=normalized.degraded_fields,
        projection_text=normalized.projection_text,
    )


# Коди помилок Meta, специфічні для шаблонів: недоступна картинка, надто довге
# поле, непідтримуваний тип. Це НЕ причина молчати — це причина відправити
# текстовий еквівалент, підготовлений разом з карточкою.
_TEMPLATE_REJECTION_MARKERS = (
    "template",
    "image_url",
    "elements",
    "attachment",
    "unsupported",
    "invalid parameter",
    "too long",
    "#100",
)


def _is_template_rejection(code: int, body: str) -> bool:
    if code == 200:
        return False
    text = str(body or "").casefold()
    return any(marker in text for marker in _TEMPLATE_REJECTION_MARKERS)


def send_template(
    settings_row,
    recipient_id: str,
    template: GenericTemplate,
    *,
    permission_boundary_factory=None,
    allow_text_fallback: bool = True,
) -> TemplateDelivery:
    """Надіслати карточку з тими ж гарантіями, що й `send_text`.

    Гарантії, які тут повторені свідомо, а не успадковані: permission boundary
    безпосередньо перед запитом, receipt-first (без `message_id` результат
    вважається невідомим і НЕ повторюється), і текстовий еквівалент при
    відхиленні карточки провайдером.
    """
    try:
        normalized = normalize_template(template)
    except TemplateValidationError as exc:
        if allow_text_fallback and _clean(template.fallback_text):
            return _text_fallback(
                settings_row,
                recipient_id,
                _clean(template.fallback_text),
                permission_boundary_factory=permission_boundary_factory,
                degraded_fields=("template_invalid",),
                projection_text=template.projection_text,
                hint=str(exc),
            )
        return TemplateDelivery(False, "invalid", str(exc))

    return _deliver_template_payload(
        settings_row,
        recipient_id,
        message=template_message_payload(normalized),
        fallback_text=normalized.fallback_text,
        degraded_fields=normalized.degraded_fields,
        projection_text=normalized.projection_text,
        permission_boundary_factory=permission_boundary_factory,
        allow_text_fallback=allow_text_fallback,
    )


def send_button_template(
    settings_row,
    recipient_id: str,
    template: ButtonTemplate,
    *,
    permission_boundary_factory=None,
    allow_text_fallback: bool = True,
) -> TemplateDelivery:
    """Send Meta's compact text-and-buttons template receipt-first."""
    try:
        normalized = normalize_button_template(template)
    except TemplateValidationError as exc:
        fallback = _clean(template.fallback_text or template.text)
        if allow_text_fallback and fallback:
            return _text_fallback(
                settings_row,
                recipient_id,
                fallback,
                permission_boundary_factory=permission_boundary_factory,
                degraded_fields=("button_template_invalid",),
                projection_text=template.projection_text,
                hint=str(exc),
            )
        return TemplateDelivery(False, "invalid", str(exc))
    return _deliver_template_payload(
        settings_row,
        recipient_id,
        message=button_template_message_payload(normalized),
        fallback_text=normalized.fallback_text,
        degraded_fields=normalized.degraded_fields,
        projection_text=normalized.projection_text,
        permission_boundary_factory=permission_boundary_factory,
        allow_text_fallback=allow_text_fallback,
    )


def send_quick_replies(
    settings_row,
    recipient_id: str,
    message: QuickReplyMessage,
    *,
    permission_boundary_factory=None,
    allow_text_fallback: bool = True,
) -> TemplateDelivery:
    """Надіслати текст із швидкими відповідями з тими ж гарантіями, що й карточку.

    Один шлях доставки для всіх трьох форм (`generic`, `button`, quick replies)
    — свідомо: receipt-first, permission boundary перед запитом і облік
    деградацій мусять бути однакові, інакше третя форма тихо втратила б
    гарантію, яку дві інші мають.
    """
    try:
        normalized = normalize_quick_reply_message(message)
    except TemplateValidationError as exc:
        fallback = _clean(message.fallback_text or message.text)
        if allow_text_fallback and fallback:
            return _text_fallback(
                settings_row,
                recipient_id,
                fallback,
                permission_boundary_factory=permission_boundary_factory,
                degraded_fields=("quick_replies_invalid",),
                projection_text=message.projection_text,
                hint=str(exc),
            )
        return TemplateDelivery(False, "invalid", str(exc))
    return _deliver_template_payload(
        settings_row,
        recipient_id,
        message=quick_reply_message_payload(normalized),
        fallback_text=normalized.fallback_text,
        degraded_fields=normalized.degraded_fields,
        projection_text=normalized.projection_text,
        permission_boundary_factory=permission_boundary_factory,
        allow_text_fallback=allow_text_fallback,
    )


def _deliver_template_payload(
    settings_row,
    recipient_id: str,
    *,
    message: dict,
    fallback_text: str,
    degraded_fields: tuple,
    projection_text: str,
    permission_boundary_factory,
    allow_text_fallback: bool,
) -> TemplateDelivery:
    from contextlib import nullcontext

    from management.services.instagram_bot import (
        _classify_send_error,
        _provider_account_id,
        _provider_http,
        _provider_message_id,
        _provider_url,
        _register_outgoing_message,
        get_page_token,
        log,
    )

    account_id = _provider_account_id(settings_row)
    page_token = get_page_token(settings_row)
    if not account_id or not page_token:
        return TemplateDelivery(False, "config", "provider_not_configured")
    body = json.dumps(
        {"recipient": {"id": recipient_id}, "message": message}
    ).encode("utf-8")
    boundary = (
        permission_boundary_factory() if permission_boundary_factory else nullcontext(True)
    )
    with boundary as allowed:
        if not allowed:
            return TemplateDelivery(False, "cancelled", "permission_changed")
        try:
            code, response = _provider_http(
                settings_row,
                _provider_url(settings_row, f"/{account_id}/messages"),
                token=page_token,
                data=body,
            )
        except Exception:
            return TemplateDelivery(
                False,
                "unknown",
                "provider_exception",
                degraded_fields=degraded_fields,
                projection_text=projection_text,
            )
    if code == 200:
        message_id = _provider_message_id(response)
        if not message_id:
            return TemplateDelivery(
                False,
                "unknown",
                "provider_message_id_missing",
                degraded_fields=degraded_fields,
                projection_text=projection_text,
            )
        _register_outgoing_message(message_id, recipient_id, kind="template")
        return TemplateDelivery(
            True,
            "sent",
            "",
            provider_message_id=message_id,
            degraded_fields=degraded_fields,
            projection_text=projection_text,
        )
    if allow_text_fallback and _is_template_rejection(code, response):
        log(
            "warning",
            "template_rejected",
            f"{recipient_id}: HTTP {code}; sending the prepared text equivalent",
        )
        return _text_fallback(
            settings_row,
            recipient_id,
            fallback_text,
            permission_boundary_factory=permission_boundary_factory,
            degraded_fields=(*degraded_fields, f"template_rejected_{code}"),
            projection_text=projection_text,
            hint=f"http_{code}",
        )
    kind, hint = _classify_send_error(code, response)
    return TemplateDelivery(
        False,
        kind,
        hint,
        degraded_fields=degraded_fields,
        projection_text=projection_text,
    )


def _text_fallback(
    settings_row,
    recipient_id: str,
    text: str,
    *,
    permission_boundary_factory,
    degraded_fields: tuple,
    projection_text: str,
    hint: str,
) -> TemplateDelivery:
    from management.services.instagram_bot import send_text

    receipt = send_text(
        settings_row,
        recipient_id,
        text,
        permission_boundary_factory=permission_boundary_factory,
        return_receipt=True,
    )
    return TemplateDelivery(
        bool(getattr(receipt, "ok", False)),
        str(getattr(receipt, "kind", "unknown") or "unknown"),
        hint or str(getattr(receipt, "hint", "") or ""),
        provider_message_id=str(getattr(receipt, "provider_message_id", "") or ""),
        used_text_fallback=True,
        degraded_fields=degraded_fields,
        projection_text=projection_text,
    )
