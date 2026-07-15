"""Publication-readiness checks for the Fable 5 editor."""

from __future__ import annotations

from copy import deepcopy

from django.core.exceptions import ValidationError

from .models import ProductOptionSizeGrid
from .size_grid_services import (
    normalize_size_grid_payload,
    resolve_effective_sizes,
)


def _issue(code: str, message: str, option_key: str = "") -> dict:
    return {"code": code, "message": message, "option_key": option_key}


def _required_option_keys(product) -> list[str]:
    notes = {note.fit_code: note for note in product.fable5_fit_notes.all()}
    keys = []
    for fit in product.fit_options.all():
        note = notes.get(fit.code)
        if fit.is_active and (note is None or note.is_enabled):
            keys.append(f"fit={fit.code}")
    if keys:
        return keys
    return list(
        ProductOptionSizeGrid.objects
        .filter(product=product)
        .order_by("id")
        .values_list("option_key", flat=True)
    )


def build_readiness(product) -> dict:
    """Return actionable errors; no implicit catalog fallback is accepted."""

    errors = []
    warnings = []
    assignments = {
        assignment.option_key: assignment
        for assignment in (
            ProductOptionSizeGrid.objects
            .filter(product=product)
            .select_related("size_grid")
        )
    }
    required_keys = _required_option_keys(product)

    for option_key in required_keys:
        assignment = assignments.get(option_key)
        if assignment is None:
            errors.append(
                _issue(
                    "missing_size_grid",
                    "Для активної посадки обов'язково оберіть розмірну сітку.",
                    option_key,
                )
            )
            continue
        grid = assignment.size_grid
        if not grid.is_active:
            errors.append(
                _issue(
                    "inactive_size_grid",
                    "Призначена розмірна сітка архівована.",
                    option_key,
                )
            )
            continue
        profile = getattr(grid, "fable5_profile", None)
        if profile is not None and not profile.is_active:
            errors.append(
                _issue(
                    "inactive_size_grid",
                    "Профіль розмірної сітки вимкнений.",
                    option_key,
                )
            )
            continue
        if profile is None:
            warnings.append(
                _issue(
                    "missing_size_grid_profile",
                    "Сітка працює, але ще не має профілю бібліотеки Fable 5.",
                    option_key,
                )
            )
        try:
            normalize_size_grid_payload(deepcopy(grid.guide_data or {}))
        except ValidationError:
            errors.append(
                _issue(
                    "invalid_size_grid",
                    "У сітці немає коректної структурованої таблиці.",
                    option_key,
                )
            )
            continue
        if not resolve_effective_sizes(product, option_key):
            errors.append(
                _issue(
                    "no_enabled_sizes",
                    "Для посадки потрібно залишити хоча б один доступний розмір.",
                    option_key,
                )
            )

    return {
        "is_ready": not errors,
        "errors": errors,
        "warnings": warnings,
        "required_option_keys": required_keys,
    }


__all__ = ["build_readiness"]
