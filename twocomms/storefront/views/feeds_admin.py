"""Staff actions and public route for marketplace feed profiles."""

from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.management import call_command
from django.core.paginator import Paginator
from django.db.models import Count
from django.db.models.deletion import ProtectedError
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from storefront.forms_feeds import MarketplaceFeedForm, MarketplaceFeedProductRuleForm
from storefront.models import MarketplaceFeed, MarketplaceFeedProductRule, Product
from storefront.services.feed_profiles import resolve_feed_rules
from storefront.services.feed_registry import FEED_ADAPTERS, build_feed_xml
from storefront.services.marketplace_feeds import build_profile_offers, resolve_base_url


SECTION = "marketplace_feeds"


def _panel_url(feed=None):
    url = f"{reverse('admin_panel')}?section={SECTION}"
    return f"{url}&feed={feed.pk}" if feed else url


def _unique_copy_slug(source: MarketplaceFeed) -> str:
    base = slugify(f"{source.slug}-copy")[:72] or "feed-copy"
    candidate = base
    index = 2
    while MarketplaceFeed.objects.filter(slug=candidate).exists():
        suffix = f"-{index}"
        candidate = f"{base[:80-len(suffix)]}{suffix}"
        index += 1
    return candidate


def build_feed_admin_context(request):
    feeds = list(
        MarketplaceFeed.objects.select_related("parent", "created_by")
        .annotate(override_count=Count("product_rules"))
        .order_by("-is_system", "name", "pk")
    )
    selected = None
    selected_id = request.GET.get("feed")
    if selected_id and str(selected_id).isdigit():
        selected = next((feed for feed in feeds if feed.pk == int(selected_id)), None)
    if selected is None and feeds:
        selected = next((feed for feed in feeds if feed.is_system), feeds[0])

    media_root = Path(getattr(settings, "MEDIA_ROOT", Path(settings.BASE_DIR) / "media"))
    feed_rows = []
    for feed in feeds:
        definition = FEED_ADAPTERS[feed.adapter]
        snapshot = media_root / definition.snapshot_name if definition.snapshot_name else None
        snapshot_exists = bool(snapshot and snapshot.exists())
        snapshot_stat = snapshot.stat() if snapshot_exists else None
        feed_rows.append(
            {
                "feed": feed,
                "definition": definition,
                "urls": definition.canonical_paths if feed.is_system else (f"/feeds/{feed.slug}.xml",),
                "snapshot_exists": snapshot_exists,
                "snapshot_size": snapshot_stat.st_size if snapshot_stat else None,
                "snapshot_updated": snapshot_stat.st_mtime if snapshot_stat else None,
                "override_count": feed.override_count,
            }
        )

    product_page = None
    product_rules = {}
    if selected:
        query = (request.GET.get("product_q") or "").strip()
        products = Product.objects.filter(status="published").prefetch_related(
            "images", "color_variants__color", "color_variants__images"
        ).order_by("title", "pk")
        if query:
            products = products.filter(title__icontains=query)
        product_page = Paginator(products, 24).get_page(request.GET.get("product_page"))
        product_rules = {
            rule.product_id: rule
            for rule in MarketplaceFeedProductRule.objects.filter(feed=selected)
        }

    return {
        "feed_rows": feed_rows,
        "selected_feed": selected,
        "selected_definition": FEED_ADAPTERS.get(selected.adapter) if selected else None,
        "selected_rules": resolve_feed_rules(selected) if selected else {},
        "feed_form": MarketplaceFeedForm(instance=selected) if selected else MarketplaceFeedForm(),
        "new_feed_form": MarketplaceFeedForm(),
        "product_page": product_page,
        "product_rules": product_rules,
        "feed_counts": {
            "total": len(feeds),
            "active": sum(feed.is_active for feed in feeds),
            "custom": sum(not feed.is_system for feed in feeds),
            "warnings": sum(not feed.is_active for feed in feeds),
        },
    }


@staff_member_required
@require_POST
def admin_marketplace_feed_create(request):
    form = MarketplaceFeedForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Не вдалося створити фід: " + "; ".join(form.errors.get_json_data().keys()))
        return redirect(_panel_url())
    feed = form.save(commit=False)
    feed.created_by = request.user
    feed.full_clean()
    feed.save()
    messages.success(request, f"Фід «{feed.name}» створено.")
    return redirect(_panel_url(feed))


@staff_member_required
@require_POST
def admin_marketplace_feed_update(request, feed_id):
    feed = get_object_or_404(MarketplaceFeed, pk=feed_id)
    form = MarketplaceFeedForm(request.POST, instance=feed)
    if form.is_valid():
        form.save()
        messages.success(request, f"Налаштування «{feed.name}» збережено.")
    else:
        messages.error(request, "Перевірте поля фіда: " + "; ".join(form.errors.keys()))
    return redirect(_panel_url(feed))


@staff_member_required
@require_POST
def admin_marketplace_feed_duplicate(request, feed_id):
    source = get_object_or_404(MarketplaceFeed, pk=feed_id)
    duplicate = MarketplaceFeed(
        name=f"{source.name} — копія",
        slug=_unique_copy_slug(source),
        adapter=source.adapter,
        language=source.language,
        parent=source,
        description=source.description,
        rules={},
        is_active=False,
        is_system=False,
        created_by=request.user,
    )
    duplicate.full_clean()
    duplicate.save()
    messages.success(request, "Копію створено вимкненою — перевірте правила перед запуском.")
    return redirect(_panel_url(duplicate))


@staff_member_required
@require_POST
def admin_marketplace_feed_delete(request, feed_id):
    feed = get_object_or_404(MarketplaceFeed, pk=feed_id)
    if feed.is_system:
        messages.error(request, "Системний фід видаляти не можна.")
        return redirect(_panel_url(feed))
    try:
        feed.delete()
    except ProtectedError:
        messages.error(request, "Фід має залежні копії. Спочатку змініть їх батьківський фід.")
        return redirect(_panel_url(feed))
    messages.success(request, "Фід видалено.")
    return redirect(_panel_url())


@staff_member_required
@require_POST
def admin_marketplace_feed_product_rule(request, feed_id):
    feed = get_object_or_404(MarketplaceFeed, pk=feed_id)
    product_id = request.POST.get("product_id")
    instance = MarketplaceFeedProductRule.objects.filter(feed=feed, product_id=product_id).first()
    form = MarketplaceFeedProductRuleForm(request.POST, instance=instance, feed=feed)
    if form.is_valid():
        form.save()
        messages.success(request, "Правило товару збережено.")
    else:
        messages.error(request, "Не вдалося зберегти правило товару: " + "; ".join(form.errors.keys()))
    return redirect(_panel_url(feed))


@staff_member_required
@require_POST
def admin_marketplace_feed_validate(request, feed_id):
    feed = get_object_or_404(MarketplaceFeed, pk=feed_id)
    try:
        feed.full_clean()
        offer_count = len(build_profile_offers(feed))
    except Exception as exc:
        messages.error(request, f"Перевірка не пройдена: {exc}")
    else:
        messages.success(request, f"Фід валідний: {offer_count} оферів готові до експорту.")
    return redirect(_panel_url(feed))


@staff_member_required
@require_POST
def admin_marketplace_feed_regenerate(request, feed_id):
    feed = get_object_or_404(MarketplaceFeed, pk=feed_id)
    definition = FEED_ADAPTERS[feed.adapter]
    if not feed.is_system or not definition.command_name:
        messages.info(request, "Кастомний фід генерується динамічно за його URL.")
        return redirect(_panel_url(feed))
    try:
        call_command("regenerate_feeds_if_dirty", force=True, min_age_sec=0, only=definition.command_name)
    except Exception:
        messages.error(request, "Не вдалося оновити snapshot. Перевірте серверний журнал.")
    else:
        messages.success(request, "Snapshot фіда оновлено.")
    return redirect(_panel_url(feed))


def custom_marketplace_feed(request, slug):
    feed = get_object_or_404(MarketplaceFeed, slug=slug, is_active=True)
    try:
        payload = build_feed_xml(feed.adapter, base_url=resolve_base_url(), feed=feed)
    except ValueError as exc:
        raise Http404 from exc
    response = HttpResponse(payload, content_type="application/xml; charset=utf-8")
    response["Content-Disposition"] = f'inline; filename="{feed.slug}.xml"'
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response["Pragma"] = "no-cache"
    response["Expires"] = "0"
    return response
