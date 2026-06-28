from __future__ import annotations

import hashlib
import json

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.db import IntegrityError, transaction
from django.db.models import Count, F, IntegerField, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseGone, HttpResponsePermanentRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.formats import date_format
from django.utils import timezone, translation
from django.utils.translation import gettext as _

from storefront.forms import BlogCategoryForm, BlogMediaAssetForm, BlogPostForm
from storefront.models import BlogCategory, BlogPost, BlogPostBlock, BlogPostView
from storefront.services.blog_blocks import create_blog_promo_claim, localize_internal_url, render_post_blocks, sync_post_blocks
from storefront.tracking import is_bot
from storefront.utm_utils import get_client_ip


BLOG_SOURCE_URL = "https://veteranfund.com.ua/stories-of-winner/artem-sinilo-istoriia-veterana/"

# Slug-и, видалені назавжди (тестові/сміттєві пости). Повертаємо 410 Gone,
# а не 404: 410 явно повідомляє пошуковику, що ресурс прибрано навмисно,
# тож Google швидше виключає URL з індексу. Тримаємо список тут, щоб 410
# працював навіть після фізичного видалення запису з БД.
GONE_BLOG_SLUGS = {"111"}

# The current BlogPost model has one canonical slug per post while locale
# prefixes provide language variants. Keep RU brief slugs as redirects so
# external Collaborator anchors from the content brief never land on 404s.
CUSTOM_PRINT_RU_SLUG_ALIASES = {
    "futbolka-so-svoim-dizaynom": "futbolka-zi-svoim-dyzainom",
    "hudi-so-svoim-printom": "hudi-zi-svoim-pryntom",
    "print-iz-foto-mema-ili-kartinki": "prynt-z-foto-memu-abo-kartynky",
    "kak-podgotovit-maket-dlya-pechati": "yak-pidhotuvaty-maket-dlya-druku",
    "razmer-printa-na-oversize-futbolku": "rozmir-pryntu-na-oversize-futbolku",
    "dtf-pechat-bez-effekta-nakleyki": "dtf-druk-bez-efektu-nakleyky",
    "dtf-dtg-ili-shelkografiya": "dtf-dtg-chy-shovkografiya",
    "futbolki-s-logotipom-dlya-brenda": "futbolky-z-logotypom-dlya-brendu",
    "kastomnaya-pechat-dlya-instagram-magazinov": "kastomnyy-druk-dlya-instagram-magazyniv",
    "futbolka-ili-hudi-s-printom-v-podarok": "futbolka-abo-hudi-z-pryntom-u-podarunok",
}


def _absolute_url(request, path: str) -> str:
    return request.build_absolute_uri(path)


def _json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False)


def _published_posts():
    return BlogPost.objects.published().order_by("-published_at", "-id")


def _active_categories():
    return BlogCategory.objects.filter(is_active=True).select_related("parent").order_by("parent__order", "order", "name")


def _posts_for_category(category: BlogCategory):
    queryset = _published_posts()
    if category.parent_id:
        return queryset.filter(category=category)
    child_ids = list(category.children.filter(is_active=True).values_list("id", flat=True))
    return queryset.filter(Q(category=category) | Q(category_id__in=child_ids))


def _timeline_months(posts: list[BlogPost]) -> list[dict[str, str]]:
    months: list[dict[str, str]] = []
    seen: set[str] = set()
    for post in posts:
        if not post.published_at:
            continue
        published_at = timezone.localtime(post.published_at)
        key = published_at.strftime("%Y-%m")
        if key in seen:
            continue
        seen.add(key)
        months.append({"key": key, "label": date_format(published_at, "F Y")})
    return months


def _blog_context(request, *, title: str | None = None) -> dict:
    categories = list(_active_categories())
    return {
        "blog_title": title or _("Новини та блог"),
        "blog_categories": categories,
        "is_blog_admin": bool(getattr(request.user, "is_staff", False)),
        "admin_blog_url": f"{reverse('admin_panel')}?section=blog",
        "admin_blog_post_create_url": reverse("admin_blog_post_create"),
        "custom_print_url": reverse("custom_print"),
    }


def _make_viewer_key(request, user_agent: str, ip: str | None) -> str:
    if getattr(request, "analytics_visitor_id", ""):
        raw = f"analytics:{request.analytics_visitor_id}"
    elif getattr(request, "session", None) and request.session.session_key:
        raw = f"session:{request.session.session_key}"
    elif getattr(request, "user", None) and request.user.is_authenticated:
        raw = f"user:{request.user.pk}"
    else:
        raw = f"anon:{ip or ''}:{user_agent}"
    return hashlib.sha256(f"{settings.SECRET_KEY}:{raw}".encode("utf-8")).hexdigest()


def record_blog_post_view(request, post: BlogPost) -> None:
    if request.method != "GET":
        return
    user_agent = (request.META.get("HTTP_USER_AGENT") or "")[:1000]
    if is_bot(user_agent):
        return

    ip = get_client_ip(request)
    visitor_key = _make_viewer_key(request, user_agent, ip)
    user = request.user if getattr(request.user, "is_authenticated", False) else None

    with transaction.atomic():
        BlogPost.objects.filter(pk=post.pk).update(view_count=F("view_count") + 1)
        try:
            view, created = BlogPostView.objects.get_or_create(
                post_id=post.pk,
                visitor_key=visitor_key,
                defaults={
                    "user": user,
                    "ip_address": ip or None,
                    "user_agent": user_agent,
                    "path": (request.path or "")[:300],
                },
            )
        except IntegrityError:
            created = False
            view = BlogPostView.objects.get(post_id=post.pk, visitor_key=visitor_key)

        if created:
            BlogPost.objects.filter(pk=post.pk).update(unique_view_count=F("unique_view_count") + 1)
        else:
            BlogPostView.objects.filter(pk=view.pk).update(
                views=F("views") + 1,
                last_seen=timezone.now(),
                user_id=(user.pk if user else view.user_id),
                path=(request.path or "")[:300],
            )


def legacy_blog_redirect(request):
    return HttpResponsePermanentRedirect(reverse("blog"))


def blog_index(request):
    posts = list(_published_posts())
    context = _blog_context(request)
    context.update(
        {
            "posts": posts,
            "featured_post": posts[0] if posts else None,
            "timeline_months": _timeline_months(posts),
            "active_category": None,
            "meta_title": _("Новини та блог | TwoComms"),
            "meta_description": _(
                "Новини TwoComms, блог бренда, огляди продукції та корисні матеріали про український одяг зі змістом."
            ),
            "canonical_path": reverse("blog"),
            "collection_schema": _json(
                {
                    "@context": "https://schema.org",
                    "@type": "CollectionPage",
                    "name": "Новини та блог TwoComms",
                    "url": _absolute_url(request, reverse("blog")),
                    "hasPart": [
                        {
                            "@type": "BlogPosting",
                            "headline": post.title,
                            "url": _absolute_url(request, post.get_absolute_url()),
                            "datePublished": post.published_at.isoformat(),
                        }
                        for post in posts[:12]
                    ],
                }
            ),
        }
    )
    return render(request, "pages/blog/index.html", context)


def blog_category(request, slug, parent_slug=None):
    if parent_slug:
        parent = get_object_or_404(_active_categories(), slug=parent_slug, parent__isnull=True)
        category = get_object_or_404(_active_categories(), slug=slug, parent=parent)
    else:
        category = get_object_or_404(_active_categories(), slug=slug)
        if category.parent_id:
            return HttpResponsePermanentRedirect(category.get_absolute_url())
    posts = list(_posts_for_category(category))
    title = category.seo_h1 or category.name
    context = _blog_context(request, title=title)
    context.update(
        {
            "posts": posts,
            # SEO 2026-06-08 (§3.4) — empty blog categories used to render
            # 200 + index,follow + sit in the sitemap → thin/soft-404 risk.
            # Emit noindex,follow while the category has no published posts;
            # it flips back to indexable automatically once a post lands.
            "noindex_page": not posts,
            "featured_post": posts[0] if posts else None,
            "timeline_months": _timeline_months(posts),
            "active_category": category,
            "meta_title": category.seo_title or f"{category.name} | Новини та блог TwoComms",
            "meta_description": category.seo_description or category.description,
            "canonical_path": category.get_absolute_url(),
            "collection_schema": _json(
                {
                    "@context": "https://schema.org",
                    "@type": "CollectionPage",
                    "name": title,
                    "description": category.seo_description or category.description,
                    "url": _absolute_url(request, category.get_absolute_url()),
                    "isPartOf": {"@type": "Blog", "name": "Новини та блог TwoComms"},
                    "hasPart": [
                        {
                            "@type": "BlogPosting",
                            "headline": post.title,
                            "url": _absolute_url(request, post.get_absolute_url()),
                            "datePublished": post.published_at.isoformat(),
                        }
                        for post in posts[:12]
                    ],
                }
            ),
        }
    )
    return render(request, "pages/blog/category.html", context)


def blog_post(request, slug):
    if slug in GONE_BLOG_SLUGS:
        return render(request, "410.html", status=410)
    alias_target = CUSTOM_PRINT_RU_SLUG_ALIASES.get(slug)
    if alias_target:
        with translation.override("ru"):
            return HttpResponsePermanentRedirect(reverse("blog_post", kwargs={"slug": alias_target}))
    post = get_object_or_404(_published_posts(), slug=slug)
    record_blog_post_view(request, post)
    post.refresh_from_db(fields=["view_count", "unique_view_count"])
    related = list(_related_posts(post))
    if len(related) < 3 and not post.category.parent_id:
        related.extend(
            list(
                _published_posts()
                .exclude(pk=post.pk)
                .exclude(pk__in=[item.pk for item in related])
                .order_by("-published_at", "-id")[: 3 - len(related)]
            )
        )

    language_code = (translation.get_language() or settings.LANGUAGE_CODE or "uk").split("-")[0]
    article_url = _absolute_url(request, post.get_absolute_url())
    article_schema = {
        "@context": "https://schema.org",
        "@type": "BlogPosting",
        "headline": post.seo_title or post.title,
        "description": post.seo_description or post.excerpt,
        "datePublished": post.published_at.isoformat(),
        "dateModified": post.updated_at.isoformat(),
        "inLanguage": language_code,
        "author": {"@type": "Organization", "name": "TwoComms"},
        "publisher": {"@type": "Organization", "name": "TwoComms"},
        "mainEntityOfPage": {"@type": "WebPage", "@id": article_url},
        "url": article_url,
        "articleSection": post.category.name,
        "isPartOf": {"@type": "Blog", "name": "Новини та блог TwoComms", "url": _absolute_url(request, reverse("blog"))},
    }
    if post.seo_keywords:
        article_schema["keywords"] = post.seo_keywords
    post_social_image_url = _absolute_url(request, post.cover_image.url) if post.cover_image else ""
    if post_social_image_url:
        article_schema["image"] = [post_social_image_url]
    blocks_html, block_schema = render_post_blocks(post, request=request)
    post_cta_url = localize_internal_url(post.cta_url, language_code) if post.cta_url else ""

    context = _blog_context(request, title=post.title)
    context.update(
        {
            "post": post,
            "post_cta_url": post_cta_url,
            "related_posts": related,
            "blocks_html": blocks_html,
            "block_schema": _json(block_schema) if block_schema else "",
            "active_category": post.category,
            "meta_title": post.seo_title or f"{post.title} | TwoComms",
            "meta_description": post.seo_description or post.excerpt,
            "canonical_path": post.get_absolute_url(),
            "post_social_image_url": post_social_image_url,
            "article_schema": _json(article_schema),
            "breadcrumb_schema": _json(
                {
                    "@context": "https://schema.org",
                    "@type": "BreadcrumbList",
                    "itemListElement": [
                        {"@type": "ListItem", "position": 1, "name": "TwoComms", "item": _absolute_url(request, reverse("home"))},
                        {"@type": "ListItem", "position": 2, "name": "Новини та блог", "item": _absolute_url(request, reverse("blog"))},
                        {"@type": "ListItem", "position": 3, "name": post.category.name, "item": _absolute_url(request, post.category.get_absolute_url())},
                        {"@type": "ListItem", "position": 4, "name": post.title, "item": _absolute_url(request, post.get_absolute_url())},
                    ],
                }
            ),
        }
    )
    return render(request, "pages/blog/post.html", context)


def _related_posts(post: BlogPost) -> list[BlogPost]:
    related: list[BlogPost] = []
    seen = {post.pk}

    def extend_from(queryset, limit):
        nonlocal related
        for item in queryset.exclude(pk__in=seen).order_by("-published_at", "-id")[:limit]:
            related.append(item)
            seen.add(item.pk)

    extend_from(_published_posts().filter(category=post.category), 3)
    if len(related) < 3 and post.category.parent_id:
        sibling_ids = list(post.category.parent.children.filter(is_active=True).values_list("id", flat=True))
        extend_from(_published_posts().filter(category_id__in=sibling_ids), 3 - len(related))
    return related


def build_admin_blog_context():
    categories = (
        BlogCategory.objects.annotate(
            posts_count=Count("posts", distinct=True),
            published_count=Count("posts", filter=Q(posts__is_published=True), distinct=True),
            draft_count=Count("posts", filter=Q(posts__is_published=False), distinct=True),
            total_views=Coalesce(Sum("posts__view_count"), Value(0), output_field=IntegerField()),
            total_unique_views=Coalesce(Sum("posts__unique_view_count"), Value(0), output_field=IntegerField()),
        )
        .order_by("order", "name")
    )
    posts = BlogPost.objects.select_related("category").order_by("-published_at", "-id")
    stats = posts.aggregate(
        total_posts=Count("id"),
        published_posts=Count("id", filter=Q(is_published=True)),
        draft_posts=Count("id", filter=Q(is_published=False)),
        posts_with_cover=Count("id", filter=Q(cover_image__isnull=False) & ~Q(cover_image="")),
        total_views=Coalesce(Sum("view_count"), Value(0), output_field=IntegerField()),
        total_unique_views=Coalesce(Sum("unique_view_count"), Value(0), output_field=IntegerField()),
    )
    stats["active_categories"] = categories.filter(is_active=True).count()
    stats["total_categories"] = categories.count()
    return {
        "blog_categories": categories,
        "blog_posts": posts,
        "blog_stats": stats,
        "blog_category_form": BlogCategoryForm(),
        "blog_post_form": BlogPostForm(),
    }


def _admin_redirect():
    return redirect(f"{reverse('admin_panel')}?section=blog")


@staff_member_required
def admin_blog_category_create(request):
    form = BlogCategoryForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Категорію блогу створено.")
        return _admin_redirect()
    return render(request, "pages/blog/editor.html", {"form": form, "editor_title": "Нова категорія блогу", "is_post_editor": False})


@staff_member_required
def admin_blog_category_update(request, pk):
    category = get_object_or_404(BlogCategory, pk=pk)
    form = BlogCategoryForm(request.POST or None, instance=category)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Категорію блогу оновлено.")
        return _admin_redirect()
    return render(request, "pages/blog/editor.html", {"form": form, "editor_title": "Редагування категорії блогу", "is_post_editor": False})


@staff_member_required
def admin_blog_category_delete(request, pk):
    category = get_object_or_404(BlogCategory, pk=pk)
    if request.method != "POST":
        return _admin_redirect()
    if category.posts.exists() or category.children.exists():
        messages.warning(request, "Категорію не видалено: спочатку перемістіть пости та дочірні категорії.")
        return _admin_redirect()
    category.delete()
    messages.success(request, "Категорію блогу видалено.")
    return _admin_redirect()


@staff_member_required
def admin_blog_post_create(request):
    form = BlogPostForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        post = form.save()
        sync_post_blocks(post, form.cleaned_data.get("blocks_json") or "")
        messages.success(request, "Пост блогу створено.")
        return _admin_redirect()
    return render(request, "pages/blog/editor.html", {"form": form, "editor_title": "Новий пост блогу", "post": None, "is_post_editor": True})


@staff_member_required
def admin_blog_post_update(request, pk):
    post = get_object_or_404(BlogPost, pk=pk)
    form = BlogPostForm(request.POST or None, request.FILES or None, instance=post)
    if request.method == "POST" and form.is_valid():
        post = form.save()
        sync_post_blocks(post, form.cleaned_data.get("blocks_json") or "")
        messages.success(request, "Пост блогу оновлено.")
        return _admin_redirect()
    return render(request, "pages/blog/editor.html", {"form": form, "editor_title": "Редагування поста блогу", "post": post, "is_post_editor": True})


@staff_member_required
def admin_blog_post_delete(request, pk):
    post = get_object_or_404(BlogPost, pk=pk)
    if request.method != "POST":
        return _admin_redirect()
    post.delete()
    messages.success(request, "Пост блогу видалено.")
    return _admin_redirect()


@staff_member_required
def admin_blog_post_preview(request, pk):
    post = get_object_or_404(BlogPost, pk=pk)
    return _render_admin_blog_preview(request, post)


@staff_member_required
def admin_blog_post_preview_new(request):
    post = BlogPost(title="Preview", slug="preview", content_html="")
    return _render_admin_blog_preview(request, post)


def _render_admin_blog_preview(request, post):
    if request.method != "POST":
        return HttpResponseBadRequest("POST required")
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except ValueError:
        return HttpResponseBadRequest("Invalid JSON")
    blocks = payload.get("blocks")
    if not isinstance(blocks, list):
        return HttpResponseBadRequest("Missing blocks")
    html, _schema = render_post_blocks(post, request=request, blocks_data=blocks)
    return HttpResponse(html)


@staff_member_required
def admin_blog_media_upload(request):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required"}, status=405)
    form = BlogMediaAssetForm(request.POST, request.FILES)
    if not form.is_valid():
        return JsonResponse({"ok": False, "errors": form.errors}, status=400)
    asset = form.save()
    return JsonResponse(
        {
            "ok": True,
            "asset": {
                "id": asset.pk,
                "url": asset.file.url,
                "width": asset.width,
                "height": asset.height,
                "alt": asset.alt,
                "caption": asset.caption,
            },
        }
    )


def blog_promo_claim(request, slug, block_id):
    post = get_object_or_404(_published_posts(), slug=slug)
    block = get_object_or_404(
        BlogPostBlock.objects.select_related("post"),
        pk=block_id,
        post=post,
        block_type=BlogPostBlock.BlockType.PROMO_CLAIM,
        is_enabled=True,
    )
    if not getattr(request.user, "is_authenticated", False):
        return redirect(f"{reverse('login')}?next={request.path}")
    if request.method != "POST":
        return redirect(post.get_absolute_url())
    try:
        with transaction.atomic():
            _claim, created = create_blog_promo_claim(post=post, block=block, user=request.user)
    except Exception:
        messages.warning(request, "Промокод недоступний або вже вичерпаний.")
        return redirect(post.get_absolute_url())
    if created:
        messages.success(request, "Промокод додано до вашого акаунта.")
    else:
        messages.info(request, "Цей промокод уже закріплений за вашим акаунтом.")
    return redirect(post.get_absolute_url())
