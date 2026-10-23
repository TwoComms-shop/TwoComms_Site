"""Deterministic product filtering and price-aware candidate ranking."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping

from .ig_catalog_graph import pricing_payload
from .ig_catalog_pricing import money_text
from .ig_commerce_types import (
    CandidateDecision,
    CatalogCandidate,
    CatalogGraph,
    CatalogProduct,
    CommerceTurnRequest,
    PriceSnapshot,
    PricingConfiguration,
)


_COLOR_FORMS = {
    "чорний": "black", "чорна": "black", "черный": "black", "черная": "black",
    "білий": "white", "біла": "white", "белый": "white", "белая": "white",
    "синій": "blue", "синя": "blue", "синий": "blue", "синяя": "blue",
    "рожевий": "pink", "рожева": "pink", "розовый": "pink", "розовая": "pink",
    "сірий": "grey", "сіра": "grey", "серый": "grey", "серая": "grey",
    "зелений": "green", "зелена": "green", "зеленый": "green", "зеленая": "green",
}
_FIT_FORMS = {
    "класика": "classic", "класична": "classic", "классика": "classic",
    "классическая": "classic", "класичний": "classic", "classic": "classic",
    "стандарт": "classic", "standard": "classic", "regular": "classic",
    "оверсайз": "oversize", "oversized": "oversize", "oversize": "oversize",
}
_GARMENT_FORMS = {
    "t-shirt": "tshirt", "t-shirts": "tshirt", "tshirt": "tshirt",
    "tshirts": "tshirt", "tee": "tshirt", "футболка": "tshirt",
    "футболки": "tshirt", "hoodie": "hoodie", "hoodies": "hoodie",
    "худі": "hoodie", "худи": "hoodie", "long-sleeve": "longsleeve",
    "longsleeve": "longsleeve", "лонгслів": "longsleeve",
    "лонгслив": "longsleeve", "sweatshirt": "sweatshirt",
    "світшот": "sweatshirt", "свитшот": "sweatshirt",
}
_STRUCTURED_FIELDS = frozenset({
    "color", "colour", "fit", "cut", "style", "size", "size_code",
    "garment_type", "category",
})


def _norm(value) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().casefold())


def _canonical(value: str) -> str:
    normalized = _norm(value)
    return _COLOR_FORMS.get(
        normalized,
        _FIT_FORMS.get(normalized, _GARMENT_FORMS.get(normalized, normalized)),
    )


def _request_constraints(request: CommerceTurnRequest) -> dict[str, str]:
    constraints: dict[str, str] = {}
    for source in (request.hard, request.semantic_constraints):
        for key, value in source.items():
            if value is not None and str(value).strip():
                constraints[str(key)] = _canonical(value)
    for key, value in request.field_updates.items():
        if key in _STRUCTURED_FIELDS and value is not None and str(value).strip():
            constraints[str(key)] = _canonical(value)
    if request.garment_type:
        constraints["garment_type"] = _canonical(request.garment_type)
    elif request.catalog_garment_type:
        constraints["garment_type"] = _canonical(request.catalog_garment_type)
    return constraints


def _configuration_matches(
    row: PricingConfiguration,
    key: str,
    expected: str,
) -> bool:
    expected = _canonical(expected)
    if key in {"color", "colour"}:
        values = {
            _canonical(row.color_slug),
            _canonical(row.color_label),
            str(row.color_id or ""),
        }
        return expected in values
    if key in {"fit", "cut", "style"}:
        return _canonical(row.fit_code or row.option_values.get("fit", "")) == expected
    if key in {"size", "size_code"}:
        return str(expected).strip().upper() in row.compatible_sizes
    if key in row.option_values:
        return _canonical(row.option_values.get(key, "")) == expected
    return True


def _filtered_pricing(
    product: CatalogProduct,
    constraints: Mapping[str, str],
) -> PriceSnapshot | None:
    pricing_keys = {
        key for key in constraints
        if key in {"color", "colour", "fit", "cut", "style", "size", "size_code"}
        or any(key in row.option_values for row in product.pricing.configurations)
    }
    if not pricing_keys:
        return product.pricing
    rows = tuple(
        row for row in product.pricing.configurations
        if all(_configuration_matches(row, key, constraints[key]) for key in pricing_keys)
    )
    if not rows:
        return None
    prices = sorted({row.price for row in rows})
    minimum = prices[0]
    maximum = prices[-1]
    exact = len(prices) == 1
    display = money_text(minimum) if exact else f"{money_text(minimum)}-{money_text(maximum)}"
    return PriceSnapshot(
        configurations=rows,
        minimum=minimum,
        maximum=maximum,
        exact=exact,
        display=display,
    )


def _alias_match(product: CatalogProduct, request: CommerceTurnRequest) -> bool:
    requested = {_norm(request.query)} if request.query else set()
    for source in (request.field_updates, request.preferences):
        requested.update(_norm(value) for value in source.values() if value)
    requested.discard("")
    aliases = {
        _norm(alias)
        for values in product.aliases.values()
        for alias in values
    }
    return bool(requested & (aliases | {_norm(product.title)}))


def _matches_non_pricing_constraint(product: CatalogProduct, key: str, expected: str) -> bool:
    expected = _canonical(expected)
    if key in {"color", "colour", "fit", "cut", "style"}:
        return True
    if key == "garment_type":
        return bool(product.garment_type) and _canonical(product.garment_type) == expected
    if key == "category":
        return expected in {
            _canonical(product.category_label),
            _canonical(product.category_slug),
        }
    if key in {"size", "size_code"}:
        return True
    if any(key in row.option_values for row in product.pricing.configurations):
        return True
    return _canonical(product.traits.get(key, "")) == expected


def _preference_matches(product: CatalogProduct, key: str, expected: str) -> bool:
    if key in {"color", "colour", "fit", "cut", "style", "size", "size_code"}:
        return _filtered_pricing(product, {key: expected}) is not None
    return _matches_non_pricing_constraint(product, key, expected)


def _preference_evidence(product: CatalogProduct, request: CommerceTurnRequest):
    matched = []
    relaxed = []
    for key, value in sorted(request.preferences.items()):
        if value is None or not str(value).strip():
            continue
        if _preference_matches(product, str(key), _canonical(value)):
            matched.append(str(key))
        else:
            relaxed.append(str(key))
    return tuple(matched), tuple(relaxed)


def _candidate(
    product: CatalogProduct,
    pricing: PriceSnapshot,
    *,
    constraints: Mapping[str, str],
    matched: int,
    exact_alias: bool,
    exact_product: bool,
    preference_matches: tuple[str, ...],
    relaxed_preferences: tuple[str, ...],
    reasons: Iterable[str],
) -> CatalogCandidate:
    return CatalogCandidate(
        product_id=product.product_id,
        slug=product.slug,
        title=product.title,
        category_id=product.category_id,
        category_slug=product.category_slug,
        category_label=product.category_label,
        garment_type=product.garment_type,
        catalog_priority=product.catalog_priority,
        traits=product.traits,
        pricing=pricing,
        constraints=tuple(sorted(constraints.items())),
        score=(
            1 if exact_product else 0,
            1 if exact_alias else 0,
            matched,
            -len(relaxed_preferences),
            int(product.catalog_priority),
            len(preference_matches),
            -int(product.product_id),
        ),
        reasons=tuple(dict.fromkeys(reasons)),
        relaxed_constraints=relaxed_preferences,
    )


def _question(candidates: tuple[CatalogCandidate, ...], constraints: Mapping[str, str]) -> str:
    if not candidates:
        return "Уточните, пожалуйста, другую модель, цвет или фасон."
    if len(candidates) == 1:
        return ""
    if "color" not in constraints and "colour" not in constraints:
        return "Какой цвет выбрать?"
    if "fit" not in constraints:
        return "Выбрать классическую посадку или оверсайз?"
    return "Какой из вариантов вам подходит?"


def _canonical_json(candidates: tuple[CatalogCandidate, ...]) -> str:
    payload = [{
        "product_id": row.product_id,
        "slug": row.slug,
        "title": row.title,
        "category": {
            "id": row.category_id,
            "slug": row.category_slug,
            "label": row.category_label,
        },
        "garment_type": row.garment_type,
        "catalog_priority": row.catalog_priority,
        "traits": dict(row.traits),
        "constraints": dict(row.constraints),
        "pricing": pricing_payload(row.pricing),
        "score": list(row.score),
        "reasons": list(row.reasons),
        "relaxed_constraints": list(row.relaxed_constraints),
    } for row in candidates]
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def rank_candidates(
    graph: CatalogGraph,
    request: CommerceTurnRequest,
    availability=None,
) -> CandidateDecision:
    """Return at most three hard-compatible, stably ordered candidates."""

    constraints = _request_constraints(request)
    exact_id = request.exact_product_id
    rows: list[CatalogCandidate] = []
    for product in graph.products:
        if exact_id is not None and product.product_id != int(exact_id):
            continue
        if any(
            not _matches_non_pricing_constraint(product, key, expected)
            for key, expected in constraints.items()
        ):
            continue
        pricing = _filtered_pricing(product, constraints)
        if pricing is None:
            continue
        if availability is not None:
            verdict = availability(product)
            if verdict in {False, "unavailable", "unknown"}:
                continue
        exact_alias = _alias_match(product, request)
        preference_matches, relaxed_preferences = _preference_evidence(product, request)
        reasons = [f"matched:{key}" for key in sorted(constraints)]
        if exact_alias:
            reasons.append("verified_alias_or_title")
        if exact_id is not None:
            reasons.append("trusted_exact_reference")
        reasons.extend(f"preference:{key}" for key in preference_matches)
        reasons.extend(f"preference_relaxed:{key}" for key in relaxed_preferences)
        rows.append(_candidate(
            product,
            pricing,
            constraints=constraints,
            matched=len(constraints),
            exact_alias=exact_alias,
            exact_product=exact_id is not None,
            preference_matches=preference_matches,
            relaxed_preferences=relaxed_preferences,
            reasons=reasons,
        ))

    rows.sort(key=lambda item: item.score, reverse=True)
    visible = tuple(rows[:3])
    unique_alias = bool(request.exact_unique_alias and len(rows) == 1)
    auto_select = bool(rows) and not visible[0].relaxed_constraints and (
        exact_id is not None
        or unique_alias
        or len(rows) == 1
    )
    return CandidateDecision(
        candidates=visible,
        auto_select=auto_select,
        selected_product_id=visible[0].product_id if auto_select else None,
        pending_question=_question(visible, constraints) if not auto_select else "",
        canonical_json=_canonical_json(visible),
        # `visible` — це сторінка, а не результат. Порядок решти кандидатів тут
        # уже відомий, і викидати його означало б, що карусель не зможе показати
        # «ще» нічого, крім тієї самої трійки.
        ordered_product_ids=tuple(row.product_id for row in rows),
    )
