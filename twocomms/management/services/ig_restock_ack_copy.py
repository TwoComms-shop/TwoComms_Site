"""Versioned presentation of notification choices, without stock authority."""
import re

VERSION = "source-choice.v1"


def attributed_label(locale, label):
    attribution = {"uk": "Ваш вибір:", "ru": "Ваш выбор:", "en": "Your choice:"}.get(locale, "Ваш вибір:")
    lines = []
    for line in label.splitlines():
        ordinal = re.match(r"^\d+\.\s+", line)
        prefix = ordinal.group(0) if ordinal else ""
        value = line[len(prefix):]
        # Choice authority cannot cross a comma, semicolon or sentence. Keep
        # the original punctuation and repeat attribution only at that boundary.
        value = re.sub(r"(?<!\d),|,(?!\d)|;|(?<!\d)\.(?=\s|$)|[!?](?=\s)|\b(?:але|но|but)\b",
            lambda match: match.group(0) + " " + attribution, value, flags=re.I)
        lines.append(prefix + attribution + " " + value)
    return "\n".join(lines)


def list_label(target):
    from management.services.ig_restock_waiting_list import item_label
    lines = []
    for index, item in enumerate(target["items"]):
        options = dict(item["options"])
        if options.get("fit") == item["fit"]:
            options.pop("fit")
        lines.append(f"{index + 1}. {item_label({**item, 'options': options})}")
    return "\n".join(lines)
