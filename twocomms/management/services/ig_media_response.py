"""Pure reply policy for caller-admitted, source-bound media interpretations.

The caller owns request/source/privacy admission and the final response truth
guard. This policy neither authorizes actions nor creates delivery receipts.
"""
from collections.abc import Mapping
import re

from management.services.ig_media_analysis import media_reaction


_PRAISE = re.compile(
    r"крут|чудов|класн|неймовір|прекрасн|вигляда\w*|выгляд\w*|"
    r"отличн|красив|гарн|стильн|супер|awesome|amazing|great|beautiful|wonderful|fantastic|"
    r"look(?:s|ing)?\s+(?:good|nice|stylish)", re.I)
_HANDOFF = re.compile(
    r"(?:переда|надісла|відправи|отправи)\w*.{0,45}(?:менедж|команд|фахів|спеціалі|специал)|"
    r"(?:менедж|фахів|спеціалі|специал).{0,45}(?:зв.яж|свяж|відпові|ответ|напиш)|"
    r"(?:forward|refer|pass(?:ed)?|sent).{0,45}(?:manager|team|specialist)|"
    r"(?:manager|team|specialist).{0,45}(?:contact|reply|respond)", re.I)
_RETRY_PROMISE = re.compile(
    r"(?:перевір|провер|переглян|посмотр|спроб|попроб|check|review|try).{0,35}"
    r"(?:пізніше|позже|знов|снова|ще\s+раз|later|again)|"
    r"(?:перевіряємо|проверяем|we\s+are\s+checking|we.re\s+checking)|"
    r"(?:повідом|сообщ|дамо\s+знати|let\s+you\s+know).{0,35}(?:щойно|коли|когда|when)", re.I)
_INSPECTION = re.compile(
    r"(?:я\s+)?(?:бачу|вижу|переглян\w*|подиви\w*|просмотр\w*|посмотрел\w*|"
    r"прослуха\w*|прослуша\w*|роздиви\w*|рассмотр\w*)|"
    r"(?:i|we)\s+(?:can\s+see|see|saw|watched|viewed|listened|inspected)", re.I)
_MEDIA_DESCRIPTION = re.compile(
    r"на\s+(?:(?:цьому|вашому|першому|этому|вашем|первом)\s+)?(?:фото|відео|видео|зображенні).{0,30}"
    r"(?:\bви\b|\bвы\b|\bє\b|видно|зображен|зображено|изображен|показан)|"
    r"(?:image|photo|video|picture)\s+(?:shows|depicts|features)", re.I)
_MEDIA_KIND_WORDS = {
    "audio": re.compile(r"голос|аудіо|аудио|voice|audio", re.I),
    "video": re.compile(r"відео|видео|video", re.I),
    "image": re.compile(r"фото|зображен|изображен|image|photo|picture", re.I),
}
_MEDIA_REWARD = re.compile(
    r"(?:за\s+(?:фото|відео|видео|відміт|отмет)|for\s+(?:the\s+|your\s+)?(?:photo|video|tag)).{0,45}"
    r"(?:зниж|скид|промокод|сертифік|сертифик|бонус|reward|discount|coupon)|"
    r"(?:нарах|начисл|earned|qualif).{0,30}(?:бонус|нагор|reward)", re.I)
_ALL_MEDIA = re.compile(r"(?:усі|всі|все|всё|весь|всю|all|every)\b", re.I)
_OUR_CLOTHES = re.compile(
    r"(?:[ву]\s+наш\w*\s+(?:одяз|одеж|футбол|худ|світш|толстов)|"
    r"наш\w*\s+(?:одяг|одежд).{0,20}(?:пасу|вам|тобі|тебе)|"
    r"(?:in|wearing)\s+our\s+(?:clothes|clothing|apparel|shirt|hoodie|sweatshirt))", re.I)
_VISUAL_PRAISE = re.compile(r"вигляда|выгляд|(?:you|your).{0,12}(?:look|outfit)|пасує|тебе\s+ид[её]т|вам\s+ид[её]т", re.I)
_SOCIAL_THANKS = re.compile(r"дяку|спасиб|thank", re.I)
_SERVICE_THANKS = re.compile(r"проблем|повідом|сообщ|уточн|детал|problem|report|detail|clarif", re.I)
_COMMERCE = re.compile(
    r"https?://|paylink|оплат|куп|замов|заказ|зниж|скид|промокод|promo|coupon|discount|"
    r"каталог|розмір|размер|продукт|товар|модель|колекц|ціна|цена|кошту|стоит|"
    r"\b(?:uah|buy|order|price|payment|catalog|size|follow)\b|грн|₴|підпис|подпис|"
    r"якщо|если|захоч|хочеш|хочете|напиш|покаж|подбер|підбер|дізна|узна|"
    r"давайте|давай|можемо|можем|оформ|обер|выбер|вибер|продовж|продолж|порад|совет", re.I)
_SERVICE_PROMOTION = re.compile(
    r"(?:ось|вот|дамо|дадим|даруємо|подарим).{0,20}(?:зниж|скид|промокод|бонус)|"
    r"(?:here\s+is|give\s+you|offer\s+you).{0,20}(?:discount|coupon|reward)|"
    r"(?:купіть|купите|buy\s+now|оформимо\s+замовлення|оформим\s+заказ)", re.I)


def _language(client):
    value = str(getattr(client, "language", "uk") or "uk").casefold()
    return 1 if value.startswith("ru") else 2 if value.startswith("en") else 0


def _localized(client, values):
    return values[_language(client)]


def _parts(analysis):
    rows = analysis.get("parts", ()) if isinstance(analysis, Mapping) else ()
    return [row for row in rows if isinstance(row, Mapping)] if isinstance(rows, (list, tuple)) else []


def _understood(rows):
    return [row for row in rows if row.get("analysis_state") == "understood"
        and row.get("outcome") == "understood" and row.get("origin") == "provider_observation"]


def _media_kinds(values):
    if isinstance(values, str):
        values = (values,)
    if not isinstance(values, (list, tuple, set, frozenset)):
        return set()
    kinds = set()
    for value in values:
        if not isinstance(value, str):
            continue
        kind = value.casefold().split("/", 1)[0]
        kind = {"voice": "audio", "photo": "image", "picture": "image"}.get(kind, kind)
        if kind in {"audio", "image", "video"}:
            kinds.add(kind)
    return kinds


def media_limitation_reply(client, *, media_kinds=()):
    """Honest type-specific clarification, without claiming a future retry."""
    kinds = _media_kinds(media_kinds)
    if kinds == {"audio"}:
        values = ("Не вдалося розібрати голосове повідомлення. Напишіть, будь ласка, його основний зміст текстом.",
            "Не удалось разобрать голосовое сообщение. Напишите, пожалуйста, его основное содержание текстом.",
            "I could not make out the voice message. Please write its main points as text.")
    elif kinds == {"video"}:
        values = ("Не вдалося розібрати відео. Опишіть, будь ласка, основне питання текстом.",
            "Не удалось разобрать видео. Опишите, пожалуйста, основной вопрос текстом.",
            "I could not make out the video. Please describe your main question as text.")
    elif kinds == {"image"}:
        values = ("Не вдалося розібрати зображення. Опишіть, будь ласка, що саме потрібно уточнити.",
            "Не удалось разобрать изображение. Опишите, пожалуйста, что именно нужно уточнить.",
            "I could not make out the image. Please describe what you would like to clarify.")
    elif len(kinds) > 1:
        values = ("Не вдалося розібрати всі вкладення. Напишіть, будь ласка, основне питання текстом.",
            "Не удалось разобрать все вложения. Напишите, пожалуйста, основной вопрос текстом.",
            "I could not make out all the attachments. Please write your main question as text.")
    else:
        values = ("Не вдалося розібрати вміст вкладення. Опишіть, будь ласка, основне питання текстом.",
            "Не удалось разобрать содержимое вложения. Опишите, пожалуйста, основной вопрос текстом.",
            "I could not make out the attachment. Please describe your main question as text.")
    return _localized(client, values)


def _thanks(client):
    return _localized(client, ("Дякуємо, що поділилися!", "Спасибо, что поделились!", "Thank you for sharing!"))


def _service_reply(client):
    return _localized(client, ("Шкода, що виникла проблема. Підкажіть, будь ласка, що саме сталося?",
        "Жаль, что возникла проблема. Подскажите, пожалуйста, что именно случилось?",
        "Sorry there is a problem. Please tell us what happened."))


def _partial_limitation_reply(client, kinds):
    """Identify a missing sibling while preserving the understood part."""
    if kinds == {"audio"}:
        values = ("Не вдалося розібрати аудіочастину повідомлення. Напишіть, будь ласка, основний зміст голосового повідомлення текстом.",
            "Не удалось разобрать аудиочасть сообщения. Напишите, пожалуйста, основное содержание голосового сообщения текстом.",
            "I could not make out the audio part of your message. Please write the voice message's main points as text.")
    elif kinds == {"video"}:
        values = ("Не вдалося розібрати відеочастину повідомлення. Опишіть, будь ласка, основне питання щодо відео текстом.",
            "Не удалось разобрать видеочасть сообщения. Опишите, пожалуйста, основной вопрос о видео текстом.",
            "I could not make out the video part of your message. Please describe your main question about the video as text.")
    elif kinds == {"image"}:
        values = ("Не вдалося розібрати частину вкладень із зображеннями. Опишіть, будь ласка, що саме потрібно уточнити щодо них.",
            "Не удалось разобрать часть вложений с изображениями. Опишите, пожалуйста, что именно нужно уточнить о них.",
            "I could not make out some image attachments. Please describe what you would like to clarify about them.")
    else:
        return media_limitation_reply(client, media_kinds=("audio", "video"))
    return _localized(client, values)


def _limitation(client, analysis, rows, understood):
    if not isinstance(analysis, Mapping):
        return ""
    # Ephemeral kinds are supplied only by the caller's fresh sealed-source
    # check. They do not belong in the persisted strict analysis schema.
    explicit = _media_kinds(analysis.get("unavailable_kinds", ()))
    unresolved = [row for row in rows if row not in understood]
    captured = analysis.get("capture_outcomes", ())
    captured = captured if isinstance(captured, (list, tuple)) else ()
    unavailable = any(isinstance(row, Mapping) and row.get("collection_outcome") != "admitted"
        for row in captured)
    if not explicit and not unresolved and not unavailable:
        return ""
    kinds = explicit | _media_kinds([row.get("mime", "") for row in unresolved])
    return _partial_limitation_reply(client, kinds) if understood else media_limitation_reply(client, media_kinds=kinds)


def _append_limitation(text, limitation):
    if not limitation or limitation in text:
        return text
    return f"{text} {limitation}" if text else limitation


def _positive_reply(client, rows):
    kinds = {row.get("content_kind") for row in rows}
    kind = next(iter(kinds)) if len(kinds) == 1 else "unknown"
    mimes = _media_kinds([row.get("mime", "") for row in rows])
    values = {
        "unboxing": ("Дякуємо, що поділилися розпакуванням!", "Спасибо, что поделились распаковкой!", "Thank you for sharing the unboxing!"),
        "wearing": ("Дякуємо, що поділилися образом!", "Спасибо, что поделились образом!", "Thank you for sharing your outfit!"),
        "product_photo": ("Дякуємо за світлину!", "Спасибо за фотографию!", "Thank you for sharing the photo!"),
        "custom_design": ("Дякуємо, що поділилися ідеєю дизайну!", "Спасибо, что поделились идеей дизайна!", "Thank you for sharing your design idea!"),
        "review_video": ("Дякуємо, що поділилися відеовідгуком!", "Спасибо, что поделились видеоотзывом!", "Thank you for sharing your video review!"),
    }
    if kind == "product_photo" and "image" not in mimes:
        return _thanks(client)
    if kind == "review_video" and "video" not in mimes:
        return _localized(client, ("Дякуємо за відгук!", "Спасибо за отзыв!", "Thank you for your review!"))
    return _localized(client, values[kind]) if kind in values else _thanks(client)


def normalize_media_reply(client, generated, *, media_analysis=None, social_only=False):
    """Preserve useful admitted replies; remove claims unsupported by media.

    ``social_only`` is for a native UGC turn without an independent caption
    obligation. Assessment/reward decisions deliberately are not an input.
    """
    text = " ".join(generated.split()) if isinstance(generated, str) else ""
    rows = _parts(media_analysis)
    understood = _understood(rows)
    try:
        reaction = media_reaction(media_analysis)
    except (TypeError, AttributeError, ValueError):
        reaction = {"mode": "neutral", "incomplete": True, "sentiments": []}
    # Tone may also come from the caller-admitted current USER caption. This
    # ephemeral hint never creates complaint_parts or changes media evidence.
    service = reaction["mode"] == "service" or (
        isinstance(media_analysis, Mapping) and media_analysis.get("source_service") is True)
    negative = service or bool({"negative", "mixed"}.intersection(reaction["sentiments"]))
    positive = reaction["mode"] == "social"
    visual_wearing = any(row.get("content_kind") == "wearing"
        and str(row.get("mime", "")).startswith(("image/", "video/")) for row in understood)
    understood_kinds = _media_kinds([row.get("mime", "") for row in understood])
    limitation = _limitation(client, media_analysis, rows, understood)
    if limitation:
        reaction["incomplete"] = True
        # Policy output may pass through winner/final normalization again.
        # Evaluate the useful answer on its own, then append the same receipt
        # of limitation once; clarification words are not a sales invitation.
        if text.endswith(limitation):
            text = text[:-len(limitation)].rstrip()
    # Keep an independent useful sentence when another sentence is unsafe.
    sentences = re.split(r"(?<=[.!?])\s+", text) if text else []
    kept = []
    for sentence in sentences:
        unsafe = bool(_HANDOFF.search(sentence) or _RETRY_PROMISE.search(sentence)
            or _OUR_CLOTHES.search(sentence) or _MEDIA_REWARD.search(sentence))
        unsafe |= bool(_INSPECTION.search(sentence) and (
            not understood or (reaction["incomplete"] and _ALL_MEDIA.search(sentence))))
        unsafe |= bool(_MEDIA_DESCRIPTION.search(sentence) and not understood)
        if _INSPECTION.search(sentence) or _MEDIA_DESCRIPTION.search(sentence):
            unsafe |= any(pattern.search(sentence) and kind not in understood_kinds
                for kind, pattern in _MEDIA_KIND_WORDS.items())
        unsafe |= bool(_PRAISE.search(sentence) and (negative or (not positive and not sentence.rstrip().endswith("?"))))
        unsafe |= bool(_VISUAL_PRAISE.search(sentence) and (not positive or not visual_wearing))
        unsafe |= bool(negative and _SOCIAL_THANKS.search(sentence) and not _SERVICE_THANKS.search(sentence))
        if not unsafe:
            kept.append(sentence.strip())
    safe = " ".join(kept)
    if service:
        result = safe if safe and len(safe) <= 700 and not _SERVICE_PROMOTION.search(safe) else _service_reply(client)
        return _append_limitation(result, limitation)
    if safe and len(safe) <= 4000:
        if not social_only:
            return _append_limitation(safe, limitation)
        if (len(safe) <= 320 and "?" not in safe and not re.search(r"\d|[%₴]", safe)
            and len(re.findall(r"[.!?]+", safe)) <= 2
            and _SOCIAL_THANKS.search(safe) and not _COMMERCE.search(safe)):
            return _append_limitation(safe, limitation)
    if positive:
        return _append_limitation(_positive_reply(client, understood), limitation)
    if limitation:
        return limitation
    if social_only:
        return _thanks(client)
    return _localized(client, ("Підкажіть, будь ласка, що саме ви хотіли б уточнити?",
        "Подскажите, пожалуйста, что именно вы хотели бы уточнить?",
        "Please tell us what you would like to clarify."))


__all__ = ["normalize_media_reply", "media_limitation_reply"]
