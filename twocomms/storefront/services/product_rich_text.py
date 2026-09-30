"""Render legacy plain or rich product copy through an explicit HTML policy."""
from html import unescape
from html.parser import HTMLParser
import re

import bleach
from django.utils.encoding import force_str
from django.utils.html import escape
from django.utils.safestring import mark_safe


ALLOWED_TAGS = (
    "p", "br", "strong", "em", "b", "i", "u", "s", "a", "ul", "ol",
    "li", "blockquote", "h2", "h3", "h4", "span", "div",
)
ALLOWED_ATTRIBUTES = {"a": ["href", "title"]}
ALLOWED_PROTOCOLS = ("http", "https", "mailto", "tel")
_HTML_TAG = re.compile(r"</?[a-zA-Z][a-zA-Z0-9:-]*(?:\s[^<>]*?)?/?>")
_DROP_CONTENT_TAGS = frozenset({"script", "style", "iframe", "object", "embed", "svg", "math", "template", "noscript", "head"})


class _WithoutExecutableContents(HTMLParser):
    """Remove script/embed bodies as well as tags before Bleach sanitization.

    This parser only removes unwanted visible payloads. Bleach performs the
    final HTML5 safety pass, including malformed markup and URI validation.
    """
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.output = []
        self.blocked = []

    def handle_starttag(self, tag, attrs):
        if tag == 'embed':
            return  # HTML void element; it has no body to suppress.
        if tag in _DROP_CONTENT_TAGS:
            self.blocked.append(tag)
        elif not self.blocked:
            self.output.append(self.get_starttag_text())

    def handle_startendtag(self, tag, attrs):
        if not self.blocked and tag not in _DROP_CONTENT_TAGS:
            self.output.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        if tag in self.blocked:
            del self.blocked[self.blocked.index(tag):]
        elif not self.blocked:
            self.output.append(f"</{tag}>")

    def handle_data(self, data):
        if not self.blocked:
            self.output.append(data)

    def handle_entityref(self, name):
        if not self.blocked:
            self.output.append(f"&{name};")

    def handle_charref(self, name):
        if not self.blocked:
            self.output.append(f"&#{name};")


def _decode_legacy_html(value):
    if _HTML_TAG.search(value):
        return value
    candidate = value
    # Some legacy rows escaped the complete editor HTML once or twice. Do
    # not decode entities inside actual rich markup or arbitrary plain text.
    for _ in range(2):
        decoded = unescape(candidate)
        if _HTML_TAG.search(decoded):
            return decoded
        if decoded == candidate:
            break
        candidate = decoded
    return value


def render_product_rich_text(value):
    """Return safe HTML for product descriptions without trusting SafeString."""
    raw = _decode_legacy_html(force_str(value) if value is not None else "")
    if not _HTML_TAG.search(raw):
        plain = escape(raw.replace("\r\n", "\n").replace("\r", "\n"))
        return mark_safe(str(plain).replace("\n", "<br>"))
    parser = _WithoutExecutableContents()
    parser.feed(raw)
    parser.close()
    cleaned = bleach.clean(
        "".join(parser.output), tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRIBUTES,
        protocols=ALLOWED_PROTOCOLS, strip=True, strip_comments=True,
    )
    return mark_safe(cleaned)
