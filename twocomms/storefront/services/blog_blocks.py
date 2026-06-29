from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html import escape
from urllib.parse import parse_qs, urlparse

import bleach
from django.conf import settings
from django.middleware.csrf import get_token
from django.urls import reverse
from django.utils import timezone, translation

from storefront.models import BlogMediaAsset, BlogPost, BlogPostBlock, BlogPromoClaim, BlogPromoCampaign, Product, ProductStatus


ALLOWED_RICH_TEXT_TAGS = [
    "p",
    "br",
    "strong",
    "em",
    "b",
    "i",
    "u",
    "s",
    "a",
    "ul",
    "ol",
    "li",
    "blockquote",
    "cite",
    "h2",
    "h3",
    "h4",
    "span",
    "div",
]
ALLOWED_RICH_TEXT_ATTRS = {
    "a": ["href", "title", "target", "rel"],
    "*": ["class"],
}


@dataclass
class RenderedBlocks:
    html: str
    schema: dict


def localized(value, language: str | None = None, fallback: str = "") -> str:
    if value is None:
        return fallback
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return str(value)
    language = (language or translation.get_language() or "uk").split("-")[0]
    for key in (language, "uk", "ru", "en"):
        raw = value.get(key)
        if raw:
            return str(raw)
    for raw in value.values():
        if raw:
            return str(raw)
    return fallback


def sanitize_rich_html(value: str) -> str:
    value = re.sub(r"<\s*(script|style)[^>]*>.*?<\s*/\s*\1\s*>", "", value or "", flags=re.I | re.S)
    value = re.sub(r"<\s*/?\s*h1[^>]*>", "", value, flags=re.I)
    cleaned = bleach.clean(
        value,
        tags=ALLOWED_RICH_TEXT_TAGS,
        attributes=ALLOWED_RICH_TEXT_ATTRS,
        protocols=["http", "https", "mailto", "tel"],
        strip=True,
    )
    return bleach.linkify(cleaned, callbacks=[_linkify_rel])


def _linkify_rel(attrs, new=False):
    href_key = (None, "href")
    href = attrs.get(href_key, "")
    if href.startswith("http"):
        attrs[(None, "target")] = "_blank"
        attrs[(None, "rel")] = "nofollow noopener"
    return attrs


def normalize_url(url: str, provider: str = "") -> str:
    url = (url or "").strip()
    if not url:
        return "#"
    if url.startswith("/") and not url.startswith("//"):
        return url
    parsed = urlparse(url)
    if parsed.scheme in {"http", "https", "mailto", "tel"}:
        return url
    if provider == "telegram":
        handle = url.lstrip("@").strip("/")
        return f"https://t.me/{handle}" if handle else "#"
    if provider == "instagram":
        handle = url.lstrip("@").strip("/")
        return f"https://www.instagram.com/{handle}/" if handle else "#"
    if provider == "tiktok":
        handle = url.lstrip("@").strip("/")
        return f"https://www.tiktok.com/@{handle}" if handle else "#"
    return "#"


def localize_internal_url(url: str, language: str | None = None) -> str:
    if not url.startswith("/") or url.startswith("//"):
        return url
    code = (language or translation.get_language() or "").split("-")[0].lower()
    default_code = str(getattr(settings, "LANGUAGE_CODE", "uk")).split("-")[0].lower()
    if not code or code == default_code:
        return url
    first_segment = url.lstrip("/").split("/", 1)[0]
    supported = {
        str(item[0]).split("-")[0].lower()
        for item in getattr(settings, "LANGUAGES", [])
        if isinstance(item, (list, tuple)) and item
    }
    if first_segment in supported or first_segment in {"admin", "admin-panel", "api", "media", "static"}:
        return url
    return f"/{code}{url}"


def external_attrs(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme in {"http", "https"}:
        return ' target="_blank" rel="nofollow noopener"'
    return ""


def css_token(value: str, fallback: str = "item") -> str:
    token = re.sub(r"[^a-z0-9_-]+", "-", str(value or "").lower()).strip("-")
    return token or fallback


def youtube_id(url: str) -> str:
    parsed = urlparse(url or "")
    if parsed.netloc.endswith("youtu.be"):
        return parsed.path.strip("/")
    if "youtube.com" in parsed.netloc:
        query = parse_qs(parsed.query)
        if query.get("v"):
            return query["v"][0]
        match = re.search(r"/embed/([^/?#]+)", parsed.path)
        if match:
            return match.group(1)
    return ""


def render_post_blocks(
    post: BlogPost,
    *,
    request=None,
    blocks_data: list[dict] | None = None,
) -> tuple[str, dict]:
    renderer = BlogBlockRenderer(post=post, request=request, blocks_data=blocks_data)
    rendered = renderer.render()
    return rendered.html, rendered.schema


class BlogBlockRenderer:
    def __init__(self, *, post: BlogPost, request=None, blocks_data: list[dict] | None = None):
        self.post = post
        self.request = request
        self.language = (translation.get_language() or "uk").split("-")[0]
        self.blocks_data = blocks_data
        self.schema: dict = {}

    def render(self) -> RenderedBlocks:
        parts: list[str] = []
        blocks = list(self._iter_blocks())
        if not blocks:
            legacy_html = sanitize_rich_html(self._legacy_content())
            return RenderedBlocks(legacy_html, {})
        for block in blocks:
            block_html = self.render_block(block)
            if block_html:
                parts.append(block_html)
        return RenderedBlocks("\n".join(parts), self.schema)

    def _legacy_content(self) -> str:
        for name in (f"content_html_{self.language}", "content_html_uk", "content_html", "content_html_ru", "content_html_en"):
            value = getattr(self.post, name, "") or ""
            if value:
                return value
        return ""

    def _iter_blocks(self):
        if self.blocks_data is not None:
            for index, item in enumerate(self.blocks_data):
                if item.get("is_enabled", True):
                    yield {
                        "id": item.get("id") or f"preview-{index}",
                        "block_type": item.get("block_type") or item.get("type"),
                        "payload": item.get("payload") or {},
                    }
            return
        queryset = self.post.blocks.filter(is_enabled=True).order_by("sort_order", "id")
        for block in queryset:
            yield {"id": block.pk, "block_type": block.block_type, "payload": block.payload or {}, "model": block}

    def render_block(self, block: dict) -> str:
        block_type = block.get("block_type")
        payload = block.get("payload") or {}
        handlers = {
            BlogPostBlock.BlockType.RICH_TEXT: self.rich_text,
            BlogPostBlock.BlockType.METRIC_CARDS: self.metric_cards,
            BlogPostBlock.BlockType.CTA_GROUP: self.cta_group,
            BlogPostBlock.BlockType.CALLOUT: self.callout,
            BlogPostBlock.BlockType.QUOTE: self.quote,
            BlogPostBlock.BlockType.FAQ: self.faq,
            BlogPostBlock.BlockType.SOURCE_LIST: self.source_list,
            BlogPostBlock.BlockType.YOUTUBE_VIDEO: self.youtube_video,
            BlogPostBlock.BlockType.LOCKED_CONTENT: self.locked_content,
            BlogPostBlock.BlockType.PROMO_CLAIM: lambda p: self.promo_claim(block, p),
            BlogPostBlock.BlockType.TABLE_SPECS: self.table_specs,
            BlogPostBlock.BlockType.PRODUCT_CTA: self.product_cta,
            BlogPostBlock.BlockType.IMAGE: self.image,
            BlogPostBlock.BlockType.GALLERY: self.gallery,
        }
        handler = handlers.get(block_type)
        if handler is None:
            return ""
        return handler(payload)

    def rich_text(self, payload: dict) -> str:
        html = sanitize_rich_html(localized(payload.get("html"), self.language))
        return f'<section class="article-structured-block article-rich-text">{html}</section>' if html else ""

    def metric_cards(self, payload: dict) -> str:
        cards_html = []
        for card in payload.get("cards") or []:
            label = escape(localized(card.get("label"), self.language))
            value = escape(str(card.get("value") or ""))
            caption = escape(localized(card.get("caption"), self.language))
            status = css_token(card.get("status") or card.get("state") or "", "")
            status_class = f" is-{status}" if status else ""
            if label or value or caption:
                cards_html.append(
                    f'<div class="article-metric-card stage-card{status_class}">'
                    f'<span class="article-metric-label">{label}</span>'
                    f'<strong class="article-metric-value">{value}</strong>'
                    f'<em class="article-metric-caption">{caption}</em>'
                    "</div>"
                )
        if not cards_html:
            return ""
        card_count = min(len(cards_html), 6)
        return (
            '<section class="article-structured-block blog-article-impact article-stage-board '
            f'article-metric-board article-metric-cards article-metric-board--count-{card_count}" '
            f'style="--metric-count:{card_count}">'
            + "".join(cards_html)
            + "</section>"
        )

    def cta_group(self, payload: dict) -> str:
        layout = css_token(payload.get("layout") or "cards", "cards")
        if layout not in {"full", "split", "cards", "compact"}:
            layout = "cards"
        extra_class = css_token(payload.get("extra_class") or payload.get("class_name") or "", "")
        extra_class_html = f" {extra_class}" if extra_class else ""
        eyebrow = escape(localized(payload.get("eyebrow") or payload.get("kicker"), self.language))
        title = escape(localized(payload.get("title"), self.language))
        body = sanitize_rich_html(localized(payload.get("body") or payload.get("description"), self.language))
        buttons = []
        for button in payload.get("buttons") or []:
            raw_provider = str(button.get("provider") or "link")
            provider = css_token(raw_provider, "link")
            style = css_token(button.get("style") or payload.get("style") or "solid", "solid")
            label = escape(localized(button.get("label"), self.language, "Відкрити"))
            caption = escape(localized(button.get("caption") or button.get("description"), self.language))
            url = localize_internal_url(normalize_url(str(button.get("url") or ""), raw_provider), self.language)
            attrs = external_attrs(url)
            icon = {
                "telegram": "fab fa-telegram",
                "instagram": "fab fa-instagram",
                "tiktok": "fab fa-tiktok",
                "product": "fas fa-shirt",
                "custom_print": "fas fa-print",
                "catalog": "fas fa-store",
                "internal": "fas fa-link",
            }.get(provider, "fas fa-arrow-right")
            buttons.append(
                f'<a href="{escape(url)}" class="article-link-card article-link-card--{provider} article-link-card--{style}" data-provider="{provider}"{attrs}>'
                f'<i class="{icon}" aria-hidden="true"></i>'
                f'<span>{label}</span><em>{caption}</em></a>'
            )
        if not buttons:
            return ""
        intro = ""
        if eyebrow or title or body:
            eyebrow_html = f"<span>{eyebrow}</span>" if eyebrow else ""
            title_html = f"<h2>{title}</h2>" if title else ""
            body_html = f"<div>{body}</div>" if body else ""
            intro = f'<div class="article-cta-copy">{eyebrow_html}{title_html}{body_html}</div>'
        return (
            f'<section class="article-structured-block article-cta-panel article-cta-row cta-layout-{layout}{extra_class_html}" '
            f'style="--cta-count:{min(len(buttons), 5)}">'
            + intro
            + "".join(buttons)
            + "</section>"
        )

    def callout(self, payload: dict) -> str:
        tone = escape(str(payload.get("tone") or "info"))
        title = escape(localized(payload.get("title"), self.language))
        body = sanitize_rich_html(localized(payload.get("body"), self.language))
        if not (title or body):
            return ""
        return (
            f'<aside class="article-structured-block article-callout type-{tone}">'
            '<div class="callout-icon"><i class="fas fa-info-circle" aria-hidden="true"></i></div>'
            f'<div class="callout-content"><strong>{title}</strong>{body}</div></aside>'
        )

    def quote(self, payload: dict) -> str:
        text = sanitize_rich_html(localized(payload.get("text"), self.language))
        cite = escape(localized(payload.get("cite"), self.language))
        if not text:
            return ""
        cite_html = f"<cite>{cite}</cite>" if cite else ""
        return f'<blockquote class="article-structured-block article-quote-monospaced"><div>{text}</div>{cite_html}</blockquote>'

    def faq(self, payload: dict) -> str:
        items_html = []
        schema_items = []
        for item in payload.get("items") or []:
            question = escape(localized(item.get("question"), self.language))
            answer = sanitize_rich_html(localized(item.get("answer"), self.language))
            if not question or not answer:
                continue
            items_html.append(f"<details><summary>{question}</summary><div>{answer}</div></details>")
            schema_items.append(
                {
                    "@type": "Question",
                    "name": question,
                    "acceptedAnswer": {"@type": "Answer", "text": re.sub(r"<[^>]+>", "", answer)},
                }
            )
        if not items_html:
            return ""
        self.schema = {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": schema_items}
        return '<section class="article-structured-block article-faq">' + "".join(items_html) + "</section>"

    def source_list(self, payload: dict) -> str:
        items = []
        for source in payload.get("sources") or []:
            label = escape(localized(source.get("label"), self.language, "Джерело"))
            url = localize_internal_url(normalize_url(str(source.get("url") or "")), self.language)
            items.append(
                f'<li><a class="article-source-link" href="{escape(url)}"{external_attrs(url)}>'
                f'<i class="fas fa-arrow-up-right-from-square" aria-hidden="true"></i>{label}</a></li>'
            )
        if not items:
            return ""
        eyebrow = escape(localized(payload.get("eyebrow"), self.language, "Першоджерела"))
        title = escape(localized(payload.get("title"), self.language, "Джерела"))
        description = escape(localized(payload.get("description"), self.language, "Посилання відкриваються у новій вкладці, якщо ведуть на зовнішній ресурс."))
        return (
            '<section class="article-structured-block article-source-card article-source-list">'
            f'<div class="article-source-copy"><span>{eyebrow}</span><h2>{title}</h2><p>{description}</p></div>'
            '<ul class="article-source-links">'
            + "".join(items)
            + "</ul></section>"
        )

    def youtube_video(self, payload: dict) -> str:
        video_id = youtube_id(str(payload.get("url") or ""))
        title = escape(localized(payload.get("title"), self.language, "YouTube video"))
        if not video_id:
            return ""
        thumb = f"https://i.ytimg.com/vi/{escape(video_id)}/hqdefault.jpg"
        return (
            '<section class="article-structured-block article-video-lite">'
            f'<button type="button" data-youtube-id="{escape(video_id)}" aria-label="{title}">'
            f'<img src="{thumb}" alt="{title}" loading="lazy" width="960" height="540">'
            '<span class="article-video-play"><i class="fas fa-play" aria-hidden="true"></i></span>'
            "</button></section>"
        )

    def locked_content(self, payload: dict) -> str:
        user = getattr(self.request, "user", None)
        if getattr(user, "is_authenticated", False):
            html = sanitize_rich_html(localized(payload.get("html"), self.language))
            return f'<section class="article-structured-block article-locked-content is-open">{html}</section>'
        teaser = escape(localized(payload.get("teaser"), self.language, "Увійдіть або зареєструйтесь, щоб побачити цей блок."))
        login_url = reverse("login")
        register_url = reverse("register")
        return (
            '<section class="article-structured-block article-locked-content is-locked">'
            f"<p>{teaser}</p><div><a href=\"{login_url}\">Увійти</a><a href=\"{register_url}\">Зареєструватися</a></div>"
            "</section>"
        )

    def promo_claim(self, block: dict, payload: dict) -> str:
        campaign_id = payload.get("campaign_id")
        title = escape(localized(payload.get("title"), self.language, "Отримати промокод"))
        label = escape(localized(payload.get("button_label"), self.language, "Забрати знижку"))
        body = escape(localized(payload.get("body"), self.language))
        code_html = ""
        user = getattr(self.request, "user", None)
        try:
            block_id = int(block.get("id"))
        except (TypeError, ValueError):
            block_id = 0
        if campaign_id and block_id and getattr(user, "is_authenticated", False):
            claim = BlogPromoClaim.objects.filter(user=user, campaign_id=campaign_id, block_id=block_id).select_related("promo_code").first()
            if claim:
                code_html = f'<strong class="article-promo-code">{escape(claim.promo_code.code)}</strong>'
        form_html = f'<button type="button" class="article-action-btn" disabled>{label}</button>'
        if block_id:
            action = reverse("blog_promo_claim", kwargs={"slug": self.post.slug, "block_id": block_id})
            csrf_html = ""
            if self.request is not None:
                csrf_html = f'<input type="hidden" name="csrfmiddlewaretoken" value="{escape(get_token(self.request))}">'
            form_html = f'<form method="post" action="{action}">{csrf_html}<button type="submit" class="article-action-btn">{label}</button></form>'
        return (
            '<section class="article-structured-block article-promo-claim">'
            f"<div><h2>{title}</h2><p>{body}</p>{code_html}</div>"
            f"{form_html}"
            "</section>"
        )

    def table_specs(self, payload: dict) -> str:
        rows = []
        for row in payload.get("rows") or []:
            key = escape(localized(row.get("label"), self.language))
            value = escape(localized(row.get("value"), self.language))
            if key or value:
                rows.append(f"<tr><th>{key}</th><td>{value}</td></tr>")
        if not rows:
            return ""
        title = escape(localized(payload.get("title"), self.language))
        title_html = f"<h2>{title}</h2>" if title else ""
        return f'<section class="article-structured-block article-spec-table article-tech-specs-table">{title_html}<table><tbody>{"".join(rows)}</tbody></table></section>'

    def product_cta(self, payload: dict) -> str:
        product_id = payload.get("product_id")
        if not product_id:
            return ""
        try:
            product = Product.objects.get(pk=product_id, status=ProductStatus.PUBLISHED)
        except (Product.DoesNotExist, TypeError, ValueError):
            return ""
        label = escape(localized(payload.get("button_label"), self.language, "Купити товар"))
        title = escape(localized(payload.get("title"), self.language, product.title))
        url = product.get_absolute_url() if hasattr(product, "get_absolute_url") else reverse("product", kwargs={"slug": product.slug})
        return (
            '<section class="article-structured-block article-product-cta">'
            f"<div><h2>{title}</h2><p>{escape(product.short_description or '')}</p></div>"
            f'<a class="article-action-btn" href="{url}">{label}</a></section>'
        )

    def image(self, payload: dict) -> str:
        media = self._media(payload.get("media_id"))
        if not media:
            return ""
        caption = escape(localized(payload.get("caption") or media.caption, self.language))
        alt = escape(localized(payload.get("alt") or media.alt, self.language, self.post.title))
        caption_html = f"<figcaption>{caption}</figcaption>" if caption else ""
        return (
            '<figure class="article-structured-block article-image-block">'
            f'<img src="{media.file.url}" alt="{alt}" loading="lazy" width="{media.width or 960}" height="{media.height or 600}">'
            f"{caption_html}</figure>"
        )

    def gallery(self, payload: dict) -> str:
        figures = []
        for item in payload.get("items") or []:
            image_html = self.image(item)
            if image_html:
                figures.append(image_html)
        if not figures:
            return ""
        return '<section class="article-structured-block article-gallery">' + "".join(figures) + "</section>"

    def _media(self, media_id):
        try:
            return BlogMediaAsset.objects.get(pk=media_id)
        except (BlogMediaAsset.DoesNotExist, TypeError, ValueError):
            return None


def block_to_admin_payload(block: BlogPostBlock) -> dict:
    return {
        "id": block.pk,
        "type": block.block_type,
        "is_enabled": block.is_enabled,
        "sort_order": block.sort_order,
        "payload": block.payload or {},
    }


def admin_blocks_json(post: BlogPost | None) -> str:
    if not post or not post.pk:
        return "[]"
    return json.dumps([block_to_admin_payload(block) for block in post.blocks.order_by("sort_order", "id")], ensure_ascii=False)


def sync_post_blocks(post: BlogPost, raw_json: str) -> None:
    if raw_json in (None, ""):
        return
    try:
        data = json.loads(raw_json)
    except (TypeError, ValueError):
        return
    if not isinstance(data, list):
        return
    keep_ids = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            continue
        block_id = item.get("id")
        block_type = item.get("type") or item.get("block_type")
        if block_type not in BlogPostBlock.BlockType.values:
            continue
        defaults = {
            "block_type": block_type,
            "sort_order": int(item.get("sort_order") or index * 10),
            "is_enabled": bool(item.get("is_enabled", True)),
            "payload": item.get("payload") if isinstance(item.get("payload"), dict) else {},
        }
        if block_id and post.blocks.filter(pk=block_id).exists():
            block, _ = BlogPostBlock.objects.update_or_create(pk=block_id, post=post, defaults=defaults)
        else:
            block = BlogPostBlock.objects.create(post=post, **defaults)
        keep_ids.append(block.pk)
    post.blocks.exclude(pk__in=keep_ids).delete()


def create_blog_promo_claim(*, post: BlogPost, block: BlogPostBlock, user):
    campaign_id = (block.payload or {}).get("campaign_id")
    campaign = BlogPromoCampaign.objects.select_for_update().get(pk=campaign_id, is_active=True)
    existing = BlogPromoClaim.objects.filter(campaign=campaign, block=block, user=user).select_related("promo_code").first()
    if existing:
        return existing, False
    if campaign.max_claims and campaign.claims.count() >= campaign.max_claims:
        raise ValueError("campaign_exhausted")
    from storefront.models import PromoCode, UserPromoCode

    code = _unique_blog_code(campaign.code_prefix)
    valid_until = timezone.now() + timezone.timedelta(days=campaign.valid_days) if campaign.valid_days else None
    promo_code = PromoCode.objects.create(
        code=code,
        promo_type="regular",
        discount_type=campaign.discount_type,
        discount_value=campaign.discount_value,
        description=f"Blog promo: {campaign.name}",
        max_uses=1,
        one_time_per_user=campaign.one_time_per_user,
        min_order_amount=campaign.min_order_amount,
        valid_from=timezone.now(),
        valid_until=valid_until,
        is_active=True,
    )
    claim = BlogPromoClaim.objects.create(
        campaign=campaign,
        post=post,
        block=block,
        user=user,
        promo_code=promo_code,
    )
    UserPromoCode.objects.get_or_create(
        user=user,
        survey_key=f"blog:{campaign.pk}:{block.pk}",
        defaults={"promo_code": promo_code, "source": "blog", "expires_at": valid_until},
    )
    return claim, True


def _unique_blog_code(prefix: str) -> str:
    from storefront.models import PromoCode

    clean_prefix = re.sub(r"[^A-Z0-9]", "", (prefix or "BLOG").upper())[:8] or "BLOG"
    for _ in range(100):
        suffix = uuid_like()
        code = f"{clean_prefix}{suffix}"[:20]
        if not PromoCode.objects.filter(code=code).exists():
            return code
    return PromoCode.generate_code(length=12)


def uuid_like() -> str:
    import uuid

    return uuid.uuid4().hex[:8].upper()
