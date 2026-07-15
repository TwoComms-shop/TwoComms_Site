"""Staff-only JSON endpoints for the reusable size-grid library."""

from __future__ import annotations

from copy import deepcopy

from django import forms
from django.db import transaction
from django.db.models import Count
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils.text import slugify
from django.views.decorators.http import require_GET, require_POST

from storefront.models import Catalog, SizeGrid

from .models import ProductOptionSizeGrid, SizeGridProfile
from .size_grid_services import (
    normalize_option_key,
    normalize_size_grid_payload,
)
from .views import _json_body, staff_api


def _image_url(grid) -> str:
    try:
        return grid.image.url if grid.image else ""
    except Exception:
        return ""


def _profile_payload(grid) -> dict:
    profile = getattr(grid, "fable5_profile", None)
    return {
        "garment_code": profile.garment_code if profile else "",
        "option_key": profile.option_key if profile else "",
        "is_active": profile.is_active if profile else True,
    }


def _grid_payload(grid, *, include_guide=True) -> dict:
    payload = {
        "id": grid.id,
        "catalog_id": grid.catalog_id,
        "catalog_name": grid.catalog.name,
        "name": grid.name,
        "description": grid.description or "",
        "image_url": _image_url(grid),
        "is_active": grid.is_active,
        "order": grid.order,
        "profile": _profile_payload(grid),
        "assigned_count": int(getattr(grid, "assigned_count", 0) or 0),
        "updated_at": grid.updated_at.isoformat() if grid.updated_at else "",
    }
    if include_guide:
        try:
            payload["guide_data"] = normalize_size_grid_payload(
                deepcopy(grid.guide_data or {})
            )
            payload["is_structured"] = True
        except forms.ValidationError:
            payload["guide_data"] = deepcopy(grid.guide_data or {})
            payload["is_structured"] = False
    return payload


def _grid_queryset():
    return (
        SizeGrid.objects
        .select_related("catalog", "fable5_profile")
        .annotate(assigned_count=Count("fable5_product_assignments"))
    )


@staff_api
@require_GET
def api_size_grids(request):
    grids = _grid_queryset()
    catalog_id = request.GET.get("catalog_id")
    if catalog_id:
        grids = grids.filter(catalog_id=catalog_id)
    if request.GET.get("include_archived") not in {"1", "true", "yes"}:
        grids = grids.filter(is_active=True)
    grids = grids.order_by("catalog__name", "order", "name", "id")
    return JsonResponse(
        {"ok": True, "grids": [_grid_payload(grid) for grid in grids]}
    )


def _clean_order(value) -> int:
    try:
        order = int(value or 0)
    except (TypeError, ValueError) as exc:
        raise forms.ValidationError("Порядок має бути числом.") from exc
    if order < 0:
        raise forms.ValidationError("Порядок не може бути від'ємним.")
    return order


def _clean_profile(raw_profile: dict | None) -> dict:
    if raw_profile is None:
        raw_profile = {}
    if not isinstance(raw_profile, dict):
        raise forms.ValidationError("Профіль сітки має бути об'єктом.")
    raw_option_key = str(raw_profile.get("option_key") or "").strip()
    option_key = normalize_option_key(raw_option_key) if raw_option_key else ""
    garment_code = slugify(str(raw_profile.get("garment_code") or ""))[:50]
    return {
        "garment_code": garment_code,
        "option_key": option_key,
        "is_active": raw_profile.get("is_active") is not False,
    }


@staff_api
@require_POST
def api_size_grid_save(request):
    payload = _json_body(request)
    catalog_id = payload.get("catalog_id")
    try:
        catalog_id = int(catalog_id)
    except (TypeError, ValueError) as exc:
        raise forms.ValidationError("Оберіть каталог.") from exc
    catalog = get_object_or_404(Catalog, pk=catalog_id)
    name = str(payload.get("name") or "").strip()
    if not name:
        raise forms.ValidationError("Вкажіть назву розмірної сітки.")
    guide_data = normalize_size_grid_payload(payload.get("guide_data"))
    profile_data = _clean_profile(payload.get("profile"))
    grid_id = payload.get("id")

    with transaction.atomic():
        if grid_id:
            grid = get_object_or_404(SizeGrid.objects.select_for_update(), pk=grid_id)
            if grid.catalog_id != catalog.id:
                raise forms.ValidationError(
                    "Каталог існуючої сітки не можна змінити. Створіть копію."
                )
        else:
            grid = SizeGrid(catalog=catalog)
        grid.name = name[:200]
        grid.description = str(payload.get("description") or "").strip()
        grid.guide_data = guide_data
        grid.order = _clean_order(payload.get("order"))
        grid.is_active = payload.get("is_active") is not False
        grid.save()
        SizeGridProfile.objects.update_or_create(
            size_grid=grid,
            defaults=profile_data,
        )

    grid = _grid_queryset().get(pk=grid.pk)
    return JsonResponse({"ok": True, "grid": _grid_payload(grid)})


def _copy_name(grid) -> str:
    base = f"{grid.name} — копія"
    names = set(
        SizeGrid.objects
        .filter(catalog_id=grid.catalog_id, name__startswith=base)
        .values_list("name", flat=True)
    )
    if base not in names:
        return base
    number = 2
    while f"{base} {number}" in names:
        number += 1
    return f"{base} {number}"


@staff_api
@require_POST
def api_size_grid_duplicate(request):
    payload = _json_body(request)
    source = get_object_or_404(
        SizeGrid.objects.select_related("fable5_profile"),
        pk=payload.get("id"),
    )
    target_catalog_id = payload.get("catalog_id") or source.catalog_id
    target_catalog = get_object_or_404(Catalog, pk=target_catalog_id)
    with transaction.atomic():
        copy = SizeGrid.objects.create(
            catalog=target_catalog,
            name=_copy_name(source) if target_catalog.id == source.catalog_id else source.name,
            description=source.description,
            guide_data=deepcopy(source.guide_data or {}),
            is_active=True,
            order=source.order,
        )
        source_profile = getattr(source, "fable5_profile", None)
        SizeGridProfile.objects.create(
            size_grid=copy,
            garment_code=source_profile.garment_code if source_profile else "",
            option_key=source_profile.option_key if source_profile else "",
            is_active=True,
        )
    copy = _grid_queryset().get(pk=copy.pk)
    return JsonResponse({"ok": True, "grid": _grid_payload(copy)})


@staff_api
@require_POST
def api_size_grid_archive(request):
    grid = get_object_or_404(SizeGrid, pk=_json_body(request).get("id"))
    if ProductOptionSizeGrid.objects.filter(size_grid=grid).exists():
        return JsonResponse(
            {
                "ok": False,
                "error": "Сітка використовується товарами. Спочатку перепризначте їх.",
                "code": "size_grid_in_use",
            },
            status=409,
        )
    grid.is_active = False
    grid.save(update_fields=["is_active", "updated_at"])
    SizeGridProfile.objects.filter(size_grid=grid).update(is_active=False)
    return JsonResponse({"ok": True, "grid_id": grid.id, "is_active": False})


@staff_api
@require_GET
def api_size_grid_preview(request):
    grid = get_object_or_404(_grid_queryset(), pk=request.GET.get("id"))
    guide = normalize_size_grid_payload(deepcopy(grid.guide_data or {}))
    return JsonResponse(
        {
            "ok": True,
            "preview": {
                **guide,
                "grid_id": grid.id,
                "grid_name": grid.name,
                "image_url": _image_url(grid),
            },
        }
    )


__all__ = [
    "api_size_grid_archive",
    "api_size_grid_duplicate",
    "api_size_grid_preview",
    "api_size_grid_save",
    "api_size_grids",
]
