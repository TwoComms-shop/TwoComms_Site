"""
Fable 5 — єдиний редактор товару (додавання = редагування) + JSON API.

Принципи:
- ОДИН шаблон для додавання і редагування. Після першого збереження
  сторінка НЕ перезавантажується — редактор просто переходить у режим
  редагування (можна одразу зберігати далі, без виходу/заходу).
- Завантаження картинок — ЗАВЖДИ append (додається в кінець), ніколи
  не перезаписує вже завантажені. Можна довантажувати з різних папок.
- Головну картинку товару можна обрати з БУДЬ-ЯКОЇ завантаженої (загальної
  чи колірної), а не лише при додаванні кольору.
- Кольори редагуються inline, без переходу на окрему сторінку.
- Старий редактор не чіпаємо — він лишається як бекап.
"""
import json
import logging
import os
import re

from django import forms
from django.db import transaction
from django.db.models import Max
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from productcolors.models import Color, ProductColorImage, ProductColorVariant
from storefront.models import (
    Catalog,
    Category,
    Product,
    ProductFAQ,
    ProductFitOption,
    ProductImage,
    SizeGrid,
)

try:  # статуси публікації (draft/review/scheduled/published/archived)
    from storefront.models import ProductStatus
    STATUS_CHOICES = [(str(v), str(l)) for v, l in ProductStatus.choices]
    DEFAULT_STATUS = str(ProductStatus.DRAFT)
except Exception:  # pragma: no cover
    ProductStatus = None
    STATUS_CHOICES = [("draft", "Чернетка"), ("published", "Опубліковано")]
    DEFAULT_STATUS = "draft"

from .models import (
    ColorProfile,
    CoverSource,
    FeedImageRule,
    FeedOnlyImage,
    FeedProductRule,
    FeedProfile,
    GarmentFlow,
    ProductFitNote,
    ProductOptionProfile,
    VariantBlankLink,
    VariantCombinationProfile,
    VariantCombinationProfileI18n,
    VariantDetails,
    VariantFAQ,
    VariantFitRule,
    VariantOptionSizeGrid,
    VariantSizeRule,
)
from .translit import smart_slugify, unique_product_slug

FIT_PRESETS = [
    {"code": "classic", "label": "Класична"},
    {"code": "oversize", "label": "Оверсайз"},
]
LANGS = ("uk", "ru", "en")
DEFAULT_SIZES = ["S", "M", "L", "XL", "XXL"]
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Допоміжне
# ---------------------------------------------------------------------------

def _is_staff(request):
    user = getattr(request, "user", None)
    return bool(user and user.is_authenticated and (user.is_staff or user.is_superuser))


def staff_api(view):
    from functools import wraps

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not _is_staff(request):
            return JsonResponse({"ok": False, "error": "Доступ лише для персоналу"}, status=403)
        try:
            return view(request, *args, **kwargs)
        except (ValueError, forms.ValidationError) as exc:
            messages = getattr(exc, "messages", None)
            error = messages[0] if messages else str(exc)
            return JsonResponse({"ok": False, "error": error}, status=400)
        except Exception:  # ніколи не віддаємо HTML-500 або внутрішні деталі в AJAX
            logger.exception("Unexpected Fable5 API error in %s", view.__name__)
            return JsonResponse(
                {"ok": False, "error": "Не вдалося виконати операцію"},
                status=400,
            )

    return wrapped


def _json_body(request):
    try:
        return json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        return {}


def _img_url(field):
    try:
        return field.url if field else ""
    except Exception:
        return ""


def _int_or_none(value):
    if value in (None, "", "null"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _alt_from_filename(name):
    stem = os.path.splitext(os.path.basename(name or ""))[0]
    return re.sub(r"[-_]+", " ", stem).strip()[:200]


def _validate_uploaded_images(files):
    """Validate actual image content before model-level ImageField saves."""
    validator = forms.ImageField()
    validated = []
    for uploaded in files:
        validator.clean(uploaded)
        try:
            uploaded.seek(0)
        except Exception:
            pass
        validated.append(uploaded)
    return validated


def _optimize_async(instance, field_name="image"):
    """Підключаємося до існуючого конвеєра оптимізації картинок (webp/avif).

    Не ламаємо збереження, якщо таска недоступна: завжди можна прогнати
    `python manage.py optimize_images` — він підхопить нові файли.
    """
    try:
        from storefront.tasks import optimize_image_field_task
    except Exception:
        return
    model = instance.__class__
    args = (model._meta.app_label, model._meta.model_name, instance.pk, field_name)
    for attr in ("delay", None):
        try:
            fn = getattr(optimize_image_field_task, attr) if attr else optimize_image_field_task
            fn(*args)
            return
        except Exception:
            continue


def _default_sizes(product=None):
    if product is not None:
        try:
            from storefront.services.size_guides import resolve_product_sizes
            sizes = resolve_product_sizes(product)
            if sizes:
                return [str(s) for s in sizes]
        except Exception:
            pass
    return list(DEFAULT_SIZES)


# ---------------------------------------------------------------------------
# Серіалізація
# ---------------------------------------------------------------------------

def _color_payload(color, profile=None):
    if profile is None:
        profile = ColorProfile.objects.filter(color=color).first()
    return {
        "id": color.id,
        "name": color.name or "",
        "primary_hex": color.primary_hex,
        "secondary_hex": color.secondary_hex or "",
        "is_thermo": bool(profile and profile.is_thermo),
        "thermo_note": (profile.thermo_note if profile else "") or "",
        "description": (profile.description if profile else "") or "",
    }


def _faq_payload(faq):
    data = {"id": faq.id, "order": faq.order, "is_active": faq.is_active}
    for fld in ("question", "answer"):
        data[fld] = getattr(faq, fld, "") or ""
        for lang in LANGS:
            data[f"{fld}_{lang}"] = getattr(faq, f"{fld}_{lang}", "") or ""
    return data


def _variant_payload(variant):
    details = VariantDetails.objects.filter(variant=variant).first()
    return {
        "id": variant.id,
        "order": variant.order,
        "is_default": variant.is_default,
        "sku": variant.sku or "",
        "price_override": variant.price_override,
        "color": _color_payload(variant.color),
        "images": [
            {"id": im.id, "url": _img_url(im.image), "alt": im.alt_text or "", "order": im.order}
            for im in variant.images.all()
        ],
        "details": {
            "display_name": details.display_name if details else "",
            "price_delta": details.price_delta if details else 0,
            "price_delta_reason": details.price_delta_reason if details else "",
            "marketing_html": details.marketing_html if details else "",
            "youtube_url": details.youtube_url if details else "",
            "seo_title": details.seo_title if details else "",
            "seo_description": details.seo_description if details else "",
            "seo_keywords": details.seo_keywords if details else "",
        },
        "fits": [
            {"fit_code": r.fit_code, "is_enabled": r.is_enabled, "reason": r.reason}
            for r in variant.fable5_fit_rules.all()
        ],
        "sizes": [
            {"fit_code": r.fit_code, "size": r.size, "is_enabled": r.is_enabled, "stock": r.stock, "note": r.note}
            for r in variant.fable5_size_rules.all()
        ],
        "size_grids": [
            {
                "option_key": assignment.option_key,
                "size_grid_id": assignment.size_grid_id,
            }
            for assignment in variant.fable5_size_grid_assignments.all().order_by("option_key", "id")
        ],
        "blank_links": [
            {
                "option_key": link.option_key,
                "storage_subcategory_id": link.storage_subcategory_id,
                "note": link.note,
            }
            for link in variant.fable5_blank_links.all().order_by("option_key", "id")
        ],
        "combinations": [
            {
                "id": profile.id,
                "combination_key": profile.combination_key,
                "option_values": profile.option_values,
                "is_active": profile.is_active,
                "price_delta": profile.price_delta,
                "price_delta_reason": profile.price_delta_reason,
                "youtube_url": profile.youtube_url,
                "content": {
                    field: getattr(content, field, "") if content else ""
                    for field in (
                        "display_name", "short_description", "full_description",
                        "marketing_text", "seo_title", "seo_description",
                        "seo_keywords", "og_title", "og_description",
                    )
                },
                "content_by_lang": {
                    row.lang: {
                        field: getattr(row, field, "")
                        for field in (
                            "display_name", "short_description", "full_description",
                            "marketing_text", "seo_title", "seo_description",
                            "seo_keywords", "og_title", "og_description",
                        )
                    }
                    for row in profile.i18n.all()
                },
            }
            for profile in variant.fable5_combinations.prefetch_related("i18n").all()
            for content in [next((row for row in profile.i18n.all() if row.lang == "uk"), None)]
        ],
        "faqs": [
            {
                "id": f.id, "order": f.order, "is_active": f.is_active,
                "question_uk": f.question_uk, "question_ru": f.question_ru, "question_en": f.question_en,
                "answer_uk": f.answer_uk, "answer_ru": f.answer_ru, "answer_en": f.answer_en,
            }
            for f in variant.fable5_faqs.all()
        ],
    }


def _product_fits_payload(product):
    notes = {n.fit_code: n for n in product.fable5_fit_notes.all()}
    options = {o.code: o for o in product.fit_options.all()}
    codes = list(dict.fromkeys([p["code"] for p in FIT_PRESETS] + list(options) + list(notes)))
    out = []
    for code in codes:
        opt = options.get(code)
        note = notes.get(code)
        label = (opt.label if opt else "") or next(
            (p["label"] for p in FIT_PRESETS if p["code"] == code), code
        )
        enabled = True
        if opt is not None:
            enabled = opt.is_active
        if note is not None:
            enabled = enabled and note.is_enabled
        out.append({
            "code": code,
            "label": label,
            "is_enabled": enabled,
            "is_default": bool(opt and opt.is_default),
            "reason": (note.reason if note else "") or "",
        })
    return out


def _product_payload(product):
    from .services import product_option_context

    cover_source = CoverSource.objects.filter(product=product).first()
    data = {
        "id": product.id,
        "title": product.title or "",
        "slug": product.slug or "",
        "category_id": product.category_id,
        "catalog_id": product.catalog_id,
        "size_grid_id": product.size_grid_id,
        "price": product.price,
        "discount_percent": product.discount_percent,
        "featured": bool(product.featured),
        "priority": getattr(product, "priority", 0) or 0,
        "points_reward": getattr(product, "points_reward", 0) or 0,
        "status": str(getattr(product, "status", "") or ""),
        "video_url": getattr(product, "video_url", "") or "",
        "short_description": product.short_description or "",
        "full_description": getattr(product, "full_description", "") or "",
        "details_text": getattr(product, "details_text", "") or "",
        "target_audience": getattr(product, "target_audience", "") or "",
        "care_instructions": getattr(product, "care_instructions", "") or "",
        "seo_title": getattr(product, "seo_title", "") or "",
        "seo_description": getattr(product, "seo_description", "") or "",
        "seo_keywords": getattr(product, "seo_keywords", "") or "",
        "main_image_alt": getattr(product, "main_image_alt", "") or "",
        "fit_selector_enabled": bool(getattr(product, "fit_selector_enabled", True)),
        "main_image_url": _img_url(product.main_image),
        "home_card_image_url": _img_url(getattr(product, "home_card_image", None)),
        "cover_source": {
            "source_type": cover_source.source_type if cover_source else "upload",
            "color_image_id": cover_source.color_image_id if cover_source else None,
            "product_image_id": cover_source.product_image_id if cover_source else None,
            "source_missing": bool(cover_source and cover_source.source_missing),
        },
        "images": [
            {"id": im.id, "url": _img_url(im.image), "alt": im.alt_text or "", "order": im.order}
            for im in product.images.all()
        ],
        "faqs": [_faq_payload(f) for f in product.faqs.all().order_by("order", "id")],
        "fits": _product_fits_payload(product),
        "option_axes": product_option_context(product).get("axes", []),
        "option_profiles": [
            {
                "option_key": profile.option_key,
                "option_values": profile.option_values,
                "is_active": profile.is_active,
                "price_delta": profile.price_delta,
                "price_delta_reason": profile.price_delta_reason,
            }
            for profile in product.fable5_option_profiles.all().order_by("option_key")
        ],
        "print_ids": list(
            product.warehouse_default_prints.order_by("id").values_list("id", flat=True)
        ),
        "variants": [
            _variant_payload(v)
            for v in product.color_variants.select_related("color").all().order_by("order", "id")
        ],
        "sizes": _default_sizes(product),
        "public_url": "",
    }
    try:
        data["public_url"] = reverse("product", args=[product.slug])
    except Exception:
        pass
    return data


def _bootstrap_payload(product=None):
    from warehouse.models import Print, StorageSubcategory

    return {
        "dictionaries": {
            "categories": [
                {"id": c.id, "name": c.name, "slug": c.slug}
                for c in Category.objects.all().order_by("order", "name")
            ],
            "catalogs": [{"id": c.id, "name": getattr(c, "name", str(c))} for c in Catalog.objects.all()],
            "size_grids": [
                {"id": g.id, "name": getattr(g, "name", str(g)), "catalog_id": getattr(g, "catalog_id", None)}
                for g in SizeGrid.objects.all()
            ],
            "storage_blanks": [
                {
                    "id": blank.id,
                    "name": str(blank),
                    "category_id": blank.category_id,
                }
                for blank in StorageSubcategory.objects.select_related("category")
                .filter(is_active=True)
                .order_by("category__order", "category__name", "order", "name")
            ],
            "prints": [
                {
                    "id": item.id,
                    "name": item.name,
                    "category": item.category.name if item.category_id else "",
                    "image_url": _img_url(item.main_image),
                    "is_active": item.is_active,
                }
                for item in Print.objects.select_related("category").order_by(
                    "category__order", "category__name", "name", "id"
                )
            ],
            "colors": [
                _color_payload(c)
                for c in Color.objects.all().prefetch_related("fable5_profile").order_by("name")
            ],
            "feeds": [
                {"id": f.id, "name": f.name, "feed_type": f.feed_type,
                 "is_active": f.is_active, "default_include": f.default_include}
                for f in FeedProfile.objects.all()
            ],
            "statuses": [{"value": v, "label": l} for v, l in STATUS_CHOICES],
            "fit_presets": FIT_PRESETS,
            "garment_flows": [
                {
                    "code": flow.code,
                    "name": flow.name,
                    "axes": flow.axes,
                    "category_ids": list(
                        flow.categories.order_by("id").values_list("id", flat=True)
                    ),
                }
                for flow in GarmentFlow.objects.filter(is_active=True)
                .prefetch_related("categories")
                .order_by("name", "code")
            ],
            "default_sizes": _default_sizes(product),
        },
        "product": _product_payload(product) if product else None,
        "urls": {
            "product_save": reverse("fable5_api_product_save"),
            "images_upload": reverse("fable5_api_images_upload"),
            "image_update": reverse("fable5_api_image_update"),
            "images_reorder": reverse("fable5_api_images_reorder"),
            "set_cover": reverse("fable5_api_set_cover"),
            "variant_save": reverse("fable5_api_variant_save"),
            "variant_delete": reverse("fable5_api_variant_delete"),
            "variant_reorder": reverse("fable5_api_variants_reorder"),
            "colors": reverse("fable5_api_colors"),
            "slug": reverse("fable5_api_slug"),
            "stock": reverse("fable5_api_stock"),
            "feeds": reverse("fable5_api_feeds"),
            "feed_create": reverse("fable5_api_feed_create"),
            "feed_rule_save": reverse("fable5_api_feed_rule_save"),
            "feed_image_upload": reverse("fable5_api_feed_image_upload"),
            "feed_image_delete": reverse("fable5_api_feed_image_delete"),
            "editor_new": reverse("fable5_product_new"),
        },
    }


# ---------------------------------------------------------------------------
# Сторінка редактора
# ---------------------------------------------------------------------------

def editor(request, product_id=None):
    if not _is_staff(request):
        return HttpResponseForbidden("Доступ лише для персоналу (staff).")
    product = get_object_or_404(Product, pk=product_id) if product_id else None
    bootstrap = _bootstrap_payload(product)
    return render(request, "fable5/editor.html", {
        "product": product,
        "bootstrap": bootstrap,
    })


# ---------------------------------------------------------------------------
# Збереження товару (створення або оновлення — один endpoint)
# ---------------------------------------------------------------------------

@staff_api
@require_POST
@transaction.atomic
def api_product_save(request):
    payload = json.loads(request.POST.get("payload") or "{}")
    _validate_uploaded_images([
        uploaded
        for key in ("main_image", "home_card_image")
        if (uploaded := request.FILES.get(key)) is not None
    ])
    product_id = _int_or_none(payload.get("id"))
    created = product_id is None
    if created:
        product = Product(status=DEFAULT_STATUS)
    else:
        product = get_object_or_404(Product, pk=product_id)

    product.title = (payload.get("title") or "").strip() or product.title or "Новий товар"

    category_id = _int_or_none(payload.get("category_id"))
    if category_id:
        product.category_id = category_id
    elif not product.category_id:
        first_cat = Category.objects.order_by("order", "id").first()
        if not first_cat:
            return JsonResponse({"ok": False, "error": "Спочатку створіть хоча б одну категорію"}, status=400)
        product.category_id = first_cat.id

    product.catalog_id = _int_or_none(payload.get("catalog_id"))
    product.size_grid_id = _int_or_none(payload.get("size_grid_id"))
    product.price = _int_or_none(payload.get("price")) or 0
    product.discount_percent = _int_or_none(payload.get("discount_percent"))
    if "featured" in payload:
        product.featured = bool(payload.get("featured"))
    if "priority" in payload and hasattr(product, "priority"):
        product.priority = _int_or_none(payload.get("priority")) or 0
    if "points_reward" in payload and hasattr(product, "points_reward"):
        product.points_reward = _int_or_none(payload.get("points_reward")) or 0
    if "fit_selector_enabled" in payload and hasattr(product, "fit_selector_enabled"):
        product.fit_selector_enabled = bool(payload.get("fit_selector_enabled"))

    status = (payload.get("status") or "").strip()
    if status and status in {v for v, _ in STATUS_CHOICES}:
        product.status = status

    for field in (
        "video_url", "short_description", "full_description", "details_text",
        "target_audience", "care_instructions", "seo_title", "seo_description",
        "seo_keywords", "main_image_alt",
    ):
        if field in payload and hasattr(product, field):
            setattr(product, field, payload.get(field) or "")

    # Переклади (modeltranslation): {"translations": {"title": {"uk": ..., "ru": ..., "en": ...}}}
    for field, values in (payload.get("translations") or {}).items():
        if not isinstance(values, dict):
            continue
        for lang, value in values.items():
            attr = f"{field}_{lang}"
            if lang in LANGS and hasattr(product, attr) and value is not None:
                setattr(product, attr, value)

    # Slug: ручний має пріоритет; інакше автогенерація з назви (КМУ-2010).
    manual_slug = (payload.get("slug") or "").strip()
    if manual_slug:
        cleaned = smart_slugify(manual_slug)
        if cleaned != product.slug:
            product.slug = unique_product_slug(cleaned, exclude_pk=product.pk)
    elif not product.slug:
        product.slug = unique_product_slug(product.title, exclude_pk=product.pk)

    # Файли головних зображень (можна також обрати з галереї — api_set_cover)
    if request.FILES.get("main_image"):
        product.main_image = request.FILES["main_image"]
    if request.FILES.get("home_card_image") and hasattr(product, "home_card_image"):
        product.home_card_image = request.FILES["home_card_image"]

    product.save()
    if request.FILES.get("main_image"):
        CoverSource.objects.update_or_create(
            product=product,
            defaults={
                "source_type": CoverSource.SourceType.UPLOAD,
                "color_image": None,
                "product_image": None,
                "source_missing": False,
            },
        )
        _optimize_async(product, "main_image")
    if request.FILES.get("home_card_image"):
        _optimize_async(product, "home_card_image")

    if "option_profiles" in payload:
        from .content_resolution import build_combination_key, normalize_option_values

        for item in payload.get("option_profiles") or []:
            values = normalize_option_values(item.get("option_values") or {})
            option_key = build_combination_key(values)
            if not option_key:
                continue
            ProductOptionProfile.objects.update_or_create(
                product=product,
                option_key=option_key,
                defaults={
                    "option_values": values,
                    "is_active": bool(item.get("is_active", True)),
                    "price_delta": _int_or_none(item.get("price_delta")),
                    "price_delta_reason": (
                        item.get("price_delta_reason") or ""
                    )[:255],
                },
            )

    if "print_ids" in payload:
        from warehouse.models import Print

        print_ids = {
            value
            for raw in payload.get("print_ids") or []
            if (value := _int_or_none(raw)) is not None
        }
        product.warehouse_default_prints.set(Print.objects.filter(pk__in=print_ids))

    # --- FAQ товару (доступні вже ПРИ ДОДАВАННІ, а не лише при редагуванні) ---
    if "faqs" in payload:
        keep_ids = []
        for index, item in enumerate(payload.get("faqs") or []):
            faq_id = _int_or_none(item.get("id"))
            faq = ProductFAQ.objects.filter(product=product, pk=faq_id).first() if faq_id else None
            if faq is None:
                faq = ProductFAQ(product=product)
            faq.question = item.get("question_uk") or item.get("question") or ""
            faq.answer = item.get("answer_uk") or item.get("answer") or ""
            for lang in LANGS:
                for fld in ("question", "answer"):
                    attr = f"{fld}_{lang}"
                    if hasattr(faq, attr) and item.get(attr) is not None:
                        setattr(faq, attr, item.get(attr))
            faq.order = index
            faq.is_active = bool(item.get("is_active", True))
            faq.save()
            keep_ids.append(faq.id)
        ProductFAQ.objects.filter(product=product).exclude(id__in=keep_ids).delete()

    # --- Посадки (класика/оверсайз): вкл/викл + причина ---
    if "fits" in payload:
        sent_codes = []
        for index, fit in enumerate(payload.get("fits") or []):
            code = (fit.get("code") or "").strip()
            if not code:
                continue
            sent_codes.append(code)
            label = fit.get("label") or next(
                (p["label"] for p in FIT_PRESETS if p["code"] == code), code
            )
            option, _ = ProductFitOption.objects.get_or_create(
                product=product, code=code, defaults={"label": label, "order": index},
            )
            option.label = label
            option.order = index
            option.is_active = bool(fit.get("is_enabled", True))
            option.save()
            note, _ = ProductFitNote.objects.get_or_create(product=product, fit_code=code)
            note.is_enabled = bool(fit.get("is_enabled", True))
            note.reason = (fit.get("reason") or "")[:255]
            note.save()
        # is_default — окремим проходом (unique constraint на дефолт)
        default_code = next(
            (f.get("code") for f in payload.get("fits") or [] if f.get("is_default")), None
        )
        if default_code:
            ProductFitOption.objects.filter(product=product, is_default=True).exclude(code=default_code).update(is_default=False)
            ProductFitOption.objects.filter(product=product, code=default_code).update(is_default=True)
        ProductFitNote.objects.filter(product=product).exclude(fit_code__in=sent_codes).delete()

    return JsonResponse({
        "ok": True,
        "created": created,
        "product": _product_payload(product),
        "edit_url": reverse("fable5_product_edit", args=[product.pk]),
    })


# ---------------------------------------------------------------------------
# Картинки: append-завантаження, alt, видалення, drag&drop, вибір обкладинки
# ---------------------------------------------------------------------------

@staff_api
@require_POST
def api_images_upload(request):
    """Завантаження ЗАВЖДИ додає в кінець і НІКОЛИ не перезаписує існуючі."""
    product = get_object_or_404(Product, pk=_int_or_none(request.POST.get("product_id")))
    target = request.POST.get("target") or "product"
    files = _validate_uploaded_images(request.FILES.getlist("files"))
    if not files:
        return JsonResponse({"ok": False, "error": "Файли не передано"}, status=400)

    created = []
    if target == "variant":
        variant = get_object_or_404(
            ProductColorVariant, pk=_int_or_none(request.POST.get("variant_id")), product=product
        )
        start = (variant.images.aggregate(m=Max("order"))["m"] or 0) + 1
        for offset, file in enumerate(files):
            image = ProductColorImage.objects.create(
                variant=variant, image=file, order=start + offset,
                alt_text=_alt_from_filename(file.name),
            )
            _optimize_async(image, "image")
            created.append({"id": image.id, "url": _img_url(image.image), "alt": image.alt_text, "order": image.order})
    else:
        start = (product.images.aggregate(m=Max("order"))["m"] or 0) + 1
        for offset, file in enumerate(files):
            image = ProductImage.objects.create(
                product=product, image=file, order=start + offset,
                alt_text=_alt_from_filename(file.name),
            )
            _optimize_async(image, "image")
            created.append({"id": image.id, "url": _img_url(image.image), "alt": image.alt_text, "order": image.order})

    return JsonResponse({"ok": True, "images": created})


def _get_image(kind, image_id, product):
    if kind == "variant":
        return get_object_or_404(ProductColorImage, pk=image_id, variant__product=product)
    return get_object_or_404(ProductImage, pk=image_id, product=product)


@staff_api
@require_POST
def api_image_update(request):
    data = _json_body(request)
    product = get_object_or_404(Product, pk=_int_or_none(data.get("product_id")))
    image = _get_image(data.get("kind") or "product", _int_or_none(data.get("id")), product)
    if data.get("delete"):
        cover = CoverSource.objects.filter(product=product).first()
        if cover and (
            (
                isinstance(image, ProductColorImage)
                and cover.color_image_id == getattr(image, "id", None)
            )
            or (
                isinstance(image, ProductImage)
                and cover.product_image_id == getattr(image, "id", None)
            )
        ):
            cover.source_missing = True
            cover.save(update_fields=["source_missing", "updated_at"])
        image.delete()
        return JsonResponse({"ok": True, "deleted": True})
    if "alt" in data:
        image.alt_text = (data.get("alt") or "")[:200]
        image.save(update_fields=["alt_text"])
    return JsonResponse({"ok": True})


@staff_api
@require_POST
def api_images_reorder(request):
    """Drag&drop порядок: ids у новому порядку -> order = індекс."""
    data = _json_body(request)
    product = get_object_or_404(Product, pk=_int_or_none(data.get("product_id")))
    ids = [i for i in (data.get("ids") or []) if _int_or_none(i) is not None]
    if (data.get("kind") or "product") == "variant":
        variant = get_object_or_404(ProductColorVariant, pk=_int_or_none(data.get("variant_id")), product=product)
        for index, image_id in enumerate(ids):
            ProductColorImage.objects.filter(pk=image_id, variant=variant).update(order=index)
    else:
        for index, image_id in enumerate(ids):
            ProductImage.objects.filter(pk=image_id, product=product).update(order=index)
    return JsonResponse({"ok": True})


@staff_api
@require_POST
def api_set_cover(request):
    """Зробити БУДЬ-ЯКУ завантажену картинку головною / карткою для головної.

    Файл не копіюється — просто перевикористовується той самий шлях у storage.
    """
    data = _json_body(request)
    product = get_object_or_404(Product, pk=_int_or_none(data.get("product_id")))
    image = _get_image(data.get("kind") or "product", _int_or_none(data.get("image_id")), product)
    target = data.get("target") or "main"
    update_fields = []
    if target == "home_card" and hasattr(product, "home_card_image"):
        product.home_card_image.name = image.image.name
        update_fields.append("home_card_image")
    else:
        product.main_image.name = image.image.name
        update_fields.append("main_image")
        if getattr(image, "alt_text", "") and hasattr(product, "main_image_alt"):
            product.main_image_alt = image.alt_text
            update_fields.append("main_image_alt")
    product.save(update_fields=update_fields)
    if target != "home_card":
        is_color_image = isinstance(image, ProductColorImage)
        CoverSource.objects.update_or_create(
            product=product,
            defaults={
                "source_type": (
                    CoverSource.SourceType.COLOR_IMAGE
                    if is_color_image
                    else CoverSource.SourceType.PRODUCT_IMAGE
                ),
                "color_image": image if is_color_image else None,
                "product_image": image if not is_color_image else None,
                "source_missing": False,
            },
        )
    return JsonResponse({
        "ok": True,
        "main_image_url": _img_url(product.main_image),
        "home_card_image_url": _img_url(getattr(product, "home_card_image", None)),
        "cover_source": _product_payload(product)["cover_source"],
    })


# ---------------------------------------------------------------------------
# Кольори (inline, без окремої сторінки) + термохром + надбавка до ціни
# ---------------------------------------------------------------------------

def _resolve_color(data):
    """Єдиний вибір/створення кольору — без подвійного вводу назви."""
    color_id = _int_or_none(data.get("id"))
    if color_id:
        color = Color.objects.get(pk=color_id)
    else:
        primary = (data.get("primary_hex") or "").strip().lower()
        if not primary.startswith("#"):
            primary = f"#{primary}"
        if not HEX_RE.match(primary):
            raise ValueError(f"Некоректний HEX: {primary}")
        secondary = (data.get("secondary_hex") or "").strip().lower() or None
        if secondary and not secondary.startswith("#"):
            secondary = f"#{secondary}"
        color, _ = Color.objects.get_or_create(
            primary_hex=primary, secondary_hex=secondary,
            defaults={"name": (data.get("name") or "").strip()},
        )
    name = (data.get("name") or "").strip()
    if name and color.name != name:
        color.name = name
        color.save(update_fields=["name"])
    # Термохром-профіль
    profile, _ = ColorProfile.objects.get_or_create(color=color)
    changed = False
    for src, dst in (("is_thermo", "is_thermo"), ("thermo_note", "thermo_note"), ("description", "description")):
        if src in data:
            value = bool(data[src]) if dst == "is_thermo" else (data.get(src) or "")
            if getattr(profile, dst) != value:
                setattr(profile, dst, value)
                changed = True
    if changed:
        profile.save()
    return color


@staff_api
def api_colors(request):
    if request.method == "POST":
        data = _json_body(request)
        color = _resolve_color(data)
        from storefront.services.catalog_helpers import bump_public_product_order_version
        transaction.on_commit(bump_public_product_order_version)
        return JsonResponse({"ok": True, "color": _color_payload(color)})
    query = (request.GET.get("q") or "").strip()
    colors = Color.objects.all().order_by("name")
    if query:
        colors = colors.filter(name__icontains=query)
    return JsonResponse({"ok": True, "colors": [_color_payload(c) for c in colors[:200]]})


@staff_api
@require_POST
@transaction.atomic
def api_variant_save(request):
    data = _json_body(request)
    product = get_object_or_404(Product, pk=_int_or_none(data.get("product_id")))
    variant_id = _int_or_none(data.get("id"))
    if variant_id:
        variant = get_object_or_404(ProductColorVariant, pk=variant_id, product=product)
    else:
        next_order = (product.color_variants.aggregate(m=Max("order"))["m"] or 0) + 1
        variant = ProductColorVariant(product=product, order=next_order)

    variant.color = _resolve_color(data.get("color") or {})
    if "sku" in data:
        variant.sku = (data.get("sku") or "")[:64]
    if "price_override" in data:
        variant.price_override = _int_or_none(data.get("price_override"))
    make_default = bool(data.get("is_default")) or not product.color_variants.exclude(pk=variant.pk).exists()
    variant.is_default = make_default
    variant.save()
    if make_default:
        product.color_variants.exclude(pk=variant.pk).update(is_default=False)

    # Деталі кольору: надбавка до ціни, per-color SEO, відео, маркетинговий опис
    details_data = data.get("details")
    if details_data is not None:
        details, _ = VariantDetails.objects.get_or_create(variant=variant)
        details.display_name = (details_data.get("display_name") or "")[:220]
        details.price_delta = _int_or_none(details_data.get("price_delta")) or 0
        details.price_delta_reason = (details_data.get("price_delta_reason") or "")[:255]
        details.marketing_html = details_data.get("marketing_html") or ""
        details.youtube_url = (details_data.get("youtube_url") or "")[:500]
        details.seo_title = (details_data.get("seo_title") or "")[:180]
        details.seo_description = (details_data.get("seo_description") or "")[:320]
        details.seo_keywords = (details_data.get("seo_keywords") or "")[:300]
        details.save()

    # Посадки для цього кольору (напр. термо — лише оверсайз)
    if "fits" in data:
        variant.fable5_fit_rules.all().delete()
        VariantFitRule.objects.bulk_create([
            VariantFitRule(
                variant=variant,
                fit_code=(f.get("fit_code") or "").strip(),
                is_enabled=bool(f.get("is_enabled", True)),
                reason=(f.get("reason") or "")[:255],
            )
            for f in data.get("fits") or [] if (f.get("fit_code") or "").strip()
        ])

    # Розміри + склад для цього кольору (напр. вимкнути S у koyote)
    if "sizes" in data:
        variant.fable5_size_rules.all().delete()
        seen = set()
        rules = []
        for s in data.get("sizes") or []:
            size = (s.get("size") or "").strip()
            fit_code = (s.get("fit_code") or "").strip()
            if not size or (fit_code, size) in seen:
                continue
            seen.add((fit_code, size))
            rules.append(VariantSizeRule(
                variant=variant, fit_code=fit_code, size=size[:12],
                is_enabled=bool(s.get("is_enabled", True)),
                stock=_int_or_none(s.get("stock")),
                note=(s.get("note") or "")[:255],
            ))
        VariantSizeRule.objects.bulk_create(rules)

    # A colour may inherit the shared product/fit grid or override it.
    if "size_grids" in data:
        from .size_grid_services import normalize_option_key

        variant.fable5_size_grid_assignments.all().delete()
        assignments = []
        seen_keys = set()
        for item in data.get("size_grids") or []:
            grid_id = _int_or_none(item.get("size_grid_id"))
            if not grid_id:
                continue
            option_key = normalize_option_key(item.get("option_key"))
            if option_key in seen_keys:
                continue
            seen_keys.add(option_key)
            assignments.append(VariantOptionSizeGrid(
                variant=variant,
                option_key=option_key,
                size_grid=get_object_or_404(SizeGrid, pk=grid_id, is_active=True),
            ))
        VariantOptionSizeGrid.objects.bulk_create(assignments)

    # The warehouse link points to a blank family/cut; concrete stock is still
    # selected by colour and size when the order is written off.
    if "blank_links" in data:
        from warehouse.models import StorageSubcategory
        from .size_grid_services import normalize_option_key

        variant.fable5_blank_links.all().delete()
        links = []
        seen_keys = set()
        for item in data.get("blank_links") or []:
            blank_id = _int_or_none(item.get("storage_subcategory_id"))
            if not blank_id:
                continue
            option_key = normalize_option_key(item.get("option_key"))
            if option_key in seen_keys:
                continue
            seen_keys.add(option_key)
            links.append(VariantBlankLink(
                variant=variant,
                option_key=option_key,
                storage_subcategory=get_object_or_404(
                    StorageSubcategory,
                    pk=blank_id,
                    is_active=True,
                ),
                note=(item.get("note") or "")[:255],
            ))
        VariantBlankLink.objects.bulk_create(links)

    # Sparse color × fit overrides. Blank fields intentionally inherit the
    # color-level VariantDetails values resolved by content_resolution.py.
    if "combinations" in data:
        from .content_resolution import build_combination_key, normalize_option_values

        content_fields = (
            "display_name", "short_description", "full_description",
            "marketing_text", "seo_title", "seo_description", "seo_keywords",
            "og_title", "og_description",
        )
        with transaction.atomic():
            existing = {
                profile.combination_key: profile
                for profile in variant.fable5_combinations.prefetch_related("i18n")
            }
            seen_keys = set()
            for item in data.get("combinations") or []:
                values = normalize_option_values(item.get("option_values") or {})
                combination_key = build_combination_key(values)
                if not combination_key or combination_key in seen_keys:
                    continue
                seen_keys.add(combination_key)
                profile = existing.get(combination_key)
                profile_values = {
                    "option_values": values,
                    "is_active": bool(item.get("is_active", True)),
                    "price_delta": _int_or_none(item.get("price_delta")),
                    "price_delta_reason": (item.get("price_delta_reason") or "")[:255],
                    "youtube_url": (item.get("youtube_url") or "")[:500],
                }
                if profile is None:
                    profile = VariantCombinationProfile.objects.create(
                        variant=variant,
                        combination_key=combination_key,
                        **profile_values,
                    )
                else:
                    for field, value in profile_values.items():
                        setattr(profile, field, value)
                    profile.save(update_fields=[*profile_values.keys(), "updated_at"])

                translations = item.get("content_by_lang")
                if not isinstance(translations, dict):
                    translations = {"uk": item.get("content") or {}}
                for lang, content in translations.items():
                    if lang not in {"uk", "ru", "en"} or not isinstance(content, dict):
                        continue
                    localized = {
                        "display_name": (content.get("display_name") or "")[:220],
                        "short_description": content.get("short_description") or "",
                        "full_description": content.get("full_description") or "",
                        "marketing_text": content.get("marketing_text") or "",
                        "seo_title": (content.get("seo_title") or "")[:180],
                        "seo_description": (content.get("seo_description") or "")[:320],
                        "seo_keywords": (content.get("seo_keywords") or "")[:300],
                        "og_title": (content.get("og_title") or "")[:180],
                        "og_description": (content.get("og_description") or "")[:320],
                    }
                    if any(str(localized[field]).strip() for field in content_fields):
                        VariantCombinationProfileI18n.objects.update_or_create(
                            profile=profile,
                            lang=lang,
                            defaults=localized,
                        )
                    else:
                        VariantCombinationProfileI18n.objects.filter(
                            profile=profile,
                            lang=lang,
                        ).delete()

            variant.fable5_combinations.exclude(
                combination_key__in=seen_keys
            ).delete()

    # FAQ для цього кольору (3 мови)
    if "faqs" in data:
        variant.fable5_faqs.all().delete()
        VariantFAQ.objects.bulk_create([
            VariantFAQ(
                variant=variant, order=index,
                is_active=bool(f.get("is_active", True)),
                question_uk=(f.get("question_uk") or "")[:255],
                question_ru=(f.get("question_ru") or "")[:255],
                question_en=(f.get("question_en") or "")[:255],
                answer_uk=f.get("answer_uk") or "",
                answer_ru=f.get("answer_ru") or "",
                answer_en=f.get("answer_en") or "",
            )
            for index, f in enumerate(data.get("faqs") or [])
            if (f.get("question_uk") or f.get("question_ru") or f.get("question_en") or "").strip()
        ])

    return JsonResponse({"ok": True, "variant": _variant_payload(variant)})


@staff_api
@require_POST
def api_variant_delete(request):
    data = _json_body(request)
    product = get_object_or_404(Product, pk=_int_or_none(data.get("product_id")))
    variant = get_object_or_404(ProductColorVariant, pk=_int_or_none(data.get("id")), product=product)
    was_default = variant.is_default
    variant.delete()
    if was_default:
        fallback = product.color_variants.order_by("order", "id").first()
        if fallback:
            fallback.is_default = True
            fallback.save(update_fields=["is_default"])
    return JsonResponse({"ok": True})


@staff_api
@require_POST
def api_variants_reorder(request):
    data = _json_body(request)
    product = get_object_or_404(Product, pk=_int_or_none(data.get("product_id")))
    for index, variant_id in enumerate(data.get("ids") or []):
        ProductColorVariant.objects.filter(pk=_int_or_none(variant_id), product=product).update(order=index)
    from storefront.services.catalog_helpers import bump_public_product_order_version
    transaction.on_commit(bump_public_product_order_version)
    return JsonResponse({"ok": True})


# ---------------------------------------------------------------------------
# Slug та склад
# ---------------------------------------------------------------------------

@staff_api
@require_POST
def api_slug_preview(request):
    data = _json_body(request)
    slug = unique_product_slug(data.get("title") or "", exclude_pk=_int_or_none(data.get("product_id")))
    return JsonResponse({"ok": True, "slug": slug})


@staff_api
def api_stock(request):
    """Зведення для адміна: залишки за розмірами у кожному кольорі товару."""
    product = get_object_or_404(Product, pk=_int_or_none(request.GET.get("product_id")))
    matrix = []
    for variant in product.color_variants.select_related("color").order_by("order", "id"):
        rules = list(variant.fable5_size_rules.all())
        matrix.append({
            "variant_id": variant.id,
            "color": _color_payload(variant.color),
            "sizes": [
                {"fit_code": r.fit_code, "size": r.size, "is_enabled": r.is_enabled, "stock": r.stock, "note": r.note}
                for r in rules
            ],
        })
    return JsonResponse({"ok": True, "default_sizes": _default_sizes(product), "matrix": matrix})


# ---------------------------------------------------------------------------
# Фіди («Селекція з фід»): участь товару, вибір картинок, фід-тільки картинки
# ---------------------------------------------------------------------------

@staff_api
def api_feeds(request):
    product = get_object_or_404(Product, pk=_int_or_none(request.GET.get("product_id")))
    rules = {}
    for rule in FeedProductRule.objects.filter(product=product).select_related("feed"):
        image_rules = [
            {
                "product_image_id": r.product_image_id,
                "color_image_id": r.color_image_id,
                "use_main_image": r.use_main_image,
                "is_allowed": r.is_allowed,
                "order": r.order,
            }
            for r in FeedImageRule.objects.filter(feed=rule.feed, product=product)
        ]
        rules[str(rule.feed_id)] = {
            "is_included": rule.is_included,
            "custom_title": rule.custom_title,
            "custom_description": rule.custom_description,
            "image_rules": image_rules,
        }
    feed_only = [
        {"id": im.id, "feed_id": im.feed_id, "url": _img_url(im.image), "alt": im.alt, "order": im.order}
        for im in FeedOnlyImage.objects.filter(product=product)
    ]
    feeds = [
        {"id": f.id, "name": f.name, "feed_type": f.feed_type,
         "is_active": f.is_active, "default_include": f.default_include}
        for f in FeedProfile.objects.all()
    ]
    return JsonResponse({"ok": True, "feeds": feeds, "rules": rules, "feed_only_images": feed_only})


@staff_api
@require_POST
def api_feed_create(request):
    data = _json_body(request)
    name = (data.get("name") or "").strip()
    if not name:
        return JsonResponse({"ok": False, "error": "Вкажіть назву фіда"}, status=400)
    base = smart_slugify(name, max_length=140) or "feed"
    slug = base
    index = 2
    while FeedProfile.objects.filter(slug=slug).exists():
        slug = f"{base}-{index}"
        index += 1
    feed = FeedProfile.objects.create(
        name=name, slug=slug,
        feed_type=data.get("feed_type") or "custom",
        default_include=bool(data.get("default_include", False)),
    )
    return JsonResponse({"ok": True, "feed": {
        "id": feed.id, "name": feed.name, "feed_type": feed.feed_type,
        "is_active": feed.is_active, "default_include": feed.default_include,
    }})


@staff_api
@require_POST
@transaction.atomic
def api_feed_rule_save(request):
    data = _json_body(request)
    product = get_object_or_404(Product, pk=_int_or_none(data.get("product_id")))
    feed = get_object_or_404(FeedProfile, pk=_int_or_none(data.get("feed_id")))
    rule, _ = FeedProductRule.objects.get_or_create(feed=feed, product=product)
    rule.is_included = bool(data.get("is_included", True))
    rule.custom_title = (data.get("custom_title") or "")[:220]
    rule.custom_description = data.get("custom_description") or ""
    rule.note = (data.get("note") or "")[:255]
    rule.save()

    if "image_rules" in data:
        image_rules = data.get("image_rules") or []
        product_image_ids = {
            _int_or_none(item.get("product_image_id"))
            for item in image_rules
            if _int_or_none(item.get("product_image_id")) is not None
        }
        color_image_ids = {
            _int_or_none(item.get("color_image_id"))
            for item in image_rules
            if _int_or_none(item.get("color_image_id")) is not None
        }
        if ProductImage.objects.filter(pk__in=product_image_ids).exclude(product=product).exists():
            raise ValueError("Картинка галереї не належить цьому товару")
        if ProductColorImage.objects.filter(pk__in=color_image_ids).exclude(variant__product=product).exists():
            raise ValueError("Картинка кольору не належить цьому товару")
        if ProductImage.objects.filter(pk__in=product_image_ids).count() != len(product_image_ids):
            raise ValueError("Картинку галереї не знайдено")
        if ProductColorImage.objects.filter(pk__in=color_image_ids).count() != len(color_image_ids):
            raise ValueError("Картинку кольору не знайдено")

        FeedImageRule.objects.filter(feed=feed, product=product).delete()
        for index, item in enumerate(image_rules):
            FeedImageRule.objects.create(
                feed=feed, product=product,
                product_image_id=_int_or_none(item.get("product_image_id")),
                color_image_id=_int_or_none(item.get("color_image_id")),
                use_main_image=bool(item.get("use_main_image", False)),
                is_allowed=bool(item.get("is_allowed", True)),
                order=_int_or_none(item.get("order")) if _int_or_none(item.get("order")) is not None else index,
            )
    return JsonResponse({"ok": True})


@staff_api
@require_POST
def api_feed_only_image_upload(request):
    """Картинка тільки для фіда — НЕ показується у картці товару."""
    product = get_object_or_404(Product, pk=_int_or_none(request.POST.get("product_id")))
    feed_id = _int_or_none(request.POST.get("feed_id"))
    feed = get_object_or_404(FeedProfile, pk=feed_id) if feed_id else None
    files = _validate_uploaded_images(request.FILES.getlist("files"))
    if not files:
        return JsonResponse({"ok": False, "error": "Файли не передано"}, status=400)
    start = (FeedOnlyImage.objects.filter(product=product).aggregate(m=Max("order"))["m"] or 0) + 1
    created = []
    for offset, file in enumerate(files):
        image = FeedOnlyImage.objects.create(
            product=product, feed=feed, image=file, order=start + offset,
            alt=_alt_from_filename(file.name),
        )
        _optimize_async(image, "image")
        created.append({"id": image.id, "feed_id": image.feed_id, "url": _img_url(image.image), "alt": image.alt, "order": image.order})
    return JsonResponse({"ok": True, "images": created})


@staff_api
@require_POST
def api_feed_only_image_delete(request):
    data = _json_body(request)
    image = get_object_or_404(FeedOnlyImage, pk=_int_or_none(data.get("id")))
    image.delete()
    return JsonResponse({"ok": True})
