"""Public text validation, shared by intake and Merchant export."""
import re
import unicodedata

CONTACT_RE = re.compile(
    r"(?:https?\s*:|www\s*\.|[\w.+-]+@[\w.-]+\.[a-z]{2,}|"
    r"\b[\w-]+\.(?:com|net|org|ua|ru|io|shop|me|xyz|online|рф|укр)\b|"
    r"(?:\+?\d[\s().-]*){10,}|<\s*/?\s*[a-z][^>]*>)", re.I,
)


def has_contact_or_markup(value):
    value = unicodedata.normalize("NFKC", value)
    value = "".join(c for c in value if unicodedata.category(c) != "Cf")
    return bool(CONTACT_RE.search(value))
