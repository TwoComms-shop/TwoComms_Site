"""Факти про готовність замовлення — щоб модель питала сама, а не за скриптом.

Причина існування модуля — конкретний прод-інцидент 02.08.2026. Клієнт писав
українською «Дай нове посилання, будь ласка», а отримував дослівно одну й ту саму
російську фразу «Подскажите, пожалуйста, какой фасон выбираете…». На питання
«Чому ти відповідаєш російською?» приходила та сама фраза ще раз.

Механіка була така: `finalize_paylink` не міг зібрати посилання, бо не знав
фасону, і **сам писав клієнту питання** з таблиці `_ASSISTED_CHECKOUT_COPY`,
підмінюючи згенеровану відповідь. Підміна потрапляла в історію діалогу як
«відповідь моделі», тому наступна генерація продовжувала чужий скрипт.

Правильний розподіл ролей інший: детермінований шар відповідає за **факти й
гарантії** (що обрано, що доступно, чи можна створювати рахунок), а формулює
завжди модель. Тому тут немає жодного тексту для клієнта — тільки службовий
блок, який кладеться в system_instruction перед генерацією.

Джерела істини навмисно ті самі, що й у каталозі (`bot_catalog`), інакше
промпт і перевірка чекауту розійшлися б: у переписці вище саме так і сталося —
каталог казав «під замовлення», а чекаут відповідав «недоступно».
"""
from __future__ import annotations

import logging
import re
from urllib.parse import unquote, urlsplit

logger = logging.getLogger(__name__)

CHECKOUT_SELECTION_CONTEXT_KEY = "assisted_checkout_selection"
LANGUAGE_REQUEST_CONTEXT_KEY = "language_request"

TRUSTED_STOREFRONT_HOSTS = frozenset({"twocomms.shop", "www.twocomms.shop"})
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_LOCALE_PREFIXES = frozenset({"uk", "ru", "en"})


def _int_or_none(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def product_reference_from_text(text: str) -> dict:
    """Товар, на який клієнт дав посилання на наш сайт.

    Клієнт #5 надіслав `https://twocomms.shop/product/classic-tshirt/` і дописав
    «Вот я за этот вариант». Це найточніше можливе висловлення вибору, але код
    його не читав узагалі: `current_product_id` лишався на попередньому товарі,
    і бот продовжував відповідати про нього.

    Повертаємо факт (`product_id`, назва, чи однозначно), а не готову відповідь:
    підтверджує зміну товару модель, своїми словами.
    """
    value = str(text or "")
    if not value:
        return {"found": False, "reason": "empty"}
    try:
        from storefront.models import Product, ProductStatus
    except Exception:
        return {"found": False, "reason": "catalog_unavailable"}

    slugs: list[str] = []
    for raw in _URL_RE.findall(value):
        parts = urlsplit(raw.rstrip(".,!?;:)]}»\"'"))
        host = (parts.hostname or "").casefold()
        if host not in TRUSTED_STOREFRONT_HOSTS:
            continue
        segments = [unquote(segment) for segment in parts.path.split("/") if segment]
        if segments and segments[0].casefold() in _LOCALE_PREFIXES:
            segments = segments[1:]
        if len(segments) < 2 or segments[0].casefold() != "product":
            continue
        slug = segments[1]
        if slug and slug not in slugs:
            slugs.append(slug)

    if not slugs:
        return {"found": False, "reason": "no_trusted_url"}

    products = list(
        Product.objects.filter(slug__in=slugs, status=ProductStatus.PUBLISHED)
        .only("id", "title", "slug")
    )
    if not products:
        # Посилання на наш домен є, але товару немає або він знятий з публікації.
        # Це теж факт, і моделі краще сказати правду, ніж вигадати наявність.
        return {"found": False, "reason": "unpublished_or_unknown", "slugs": slugs}
    if len(products) > 1:
        return {
            "found": False,
            "reason": "multiple_products",
            "candidates": [
                {"product_id": product.pk, "title": product.title} for product in products
            ],
        }
    product = products[0]
    return {
        "found": True,
        "product_id": product.pk,
        "title": product.title,
        "slug": product.slug,
    }


def _selection_state(client, product_id):
    context = getattr(client, "sales_context", None)
    if not isinstance(context, dict):
        return {}
    state = context.get(CHECKOUT_SELECTION_CONTEXT_KEY)
    if not isinstance(state, dict):
        return {}
    stored_product_id = _int_or_none(state.get("product_id"))
    if product_id and stored_product_id and stored_product_id != product_id:
        return {}
    return dict(state)


def _fit_rows(product, *, strict=False):
    try:
        from storefront.models import ProductFitOption

        return list(
            ProductFitOption.objects.filter(product=product, is_active=True)
            .order_by("order", "id")
        )
    except Exception:
        if strict:
            raise
        return []


def sizes_for_fit(product, fit_code: str, *, variant=None, strict=False) -> dict:
    """Розміри для конкретного фасону: доступні окремо, вимкнені окремо.

    Вимкнені розміри потрібні як факт: коли клієнт просить M, а M вимкнений,
    правильна відповідь — «подивилась, цього розміру немає, уточню в менеджера»,
    і сказати її має модель. Для цього вона мусить бачити різницю між «розміру
    немає в сітці взагалі» і «розмір є, але зараз вимкнений».
    """
    available: list[str] = []
    disabled: list[str] = []
    try:
        from product_catalog.size_grid_services import normalize_size_value, resolve_effective_sizes
        from storefront.services.size_guides import resolve_product_sizes

        if fit_code:
            # Варіант передаємо, коли він відомий: правила `VariantSizeRule`
            # живуть на рівні кольору.
            for row in resolve_effective_sizes(product, f"fit={fit_code}", variant=variant):
                if not isinstance(row, dict):
                    continue
                size = normalize_size_value(row.get("size"))
                if size and size not in available:
                    available.append(size)
        if not available:
            # Товар без окремої сітки під фасон — беремо загальну.
            if strict:
                from storefront.services.size_guides import (
                    _ordered_size_values_from_catalog, _resolve_size_grid_source,
                )
                fallback_sizes = _ordered_size_values_from_catalog(product)
                if not fallback_sizes:
                    fallback_grid, _ = _resolve_size_grid_source(product)
                    if fallback_grid is not None and fallback_grid.image:
                        # The legacy guide serializer reads image dimensions
                        # from storage. Optional journey facts must stay SQL-only.
                        raise ValueError("image_backed_size_guide_unsupported")
                    fallback_sizes = resolve_product_sizes(product)
            else:
                fallback_sizes = resolve_product_sizes(product)
            for value in fallback_sizes:
                size = normalize_size_value(value)
                if size and size not in available:
                    available.append(size)
        # `resolve_effective_sizes` віддає лише увімкнені рядки, тому вимкнені
        # доводиться питати окремо. Без цього кроку вимкнений менеджером розмір
        # виглядав би доступним, і бот пообіцяв би те, чого немає — а нам
        # потрібно рівно протилежне: сказати правду й запропонувати наступний крок.
        disabled = _disabled_sizes(variant, fit_code, **({"strict": True} if strict else {}))
        available = [size for size in available if size not in disabled]
    except Exception as exc:  # noqa: BLE001 - факт відсутності теж факт
        if strict:
            raise
        logger.warning("ig size grid unavailable for product %s: %r", getattr(product, "pk", None), exc)
        return {"available": [], "disabled": [], "resolved": False}
    return {"available": available, "disabled": disabled, "resolved": True}


def _disabled_sizes(variant, fit_code: str, *, strict=False) -> list[str]:
    """Розміри, вимкнені для цього кольору (загальні + для конкретного фасону)."""
    if variant is None:
        return []
    try:
        from product_catalog.models import VariantSizeRule
        from product_catalog.size_grid_services import normalize_size_value

        rules = VariantSizeRule.objects.filter(variant=variant)
        if fit_code:
            rules = rules.filter(fit_code__in=("", fit_code))
        else:
            rules = rules.filter(fit_code="")
        result: list[str] = []
        for rule in rules:
            unavailable = not rule.is_enabled or (
                rule.stock is not None and int(rule.stock) <= 0
            )
            if not unavailable:
                continue
            size = normalize_size_value(rule.size)
            if size and size not in result:
                result.append(size)
        return result
    except Exception:
        if strict:
            raise
        return []


def _color_rows(product, *, fit_code: str, size: str, option_values=None, strict=False):
    """Кольори, які реально можна купити за правилами вітрини.

    Свідомо **не** фільтруємо за числовим `ProductColorVariant.stock`: на проді
    `stock > 0` лише в 1 варіанта з 81, а сайт продає всі 71 опублікований товар.
    Тобто це поле в цьому проєкті не є джерелом істини про наявність, і саме
    воно давало «Выбранный вариант сейчас недоступен» на кожен товар.
    """
    try:
        from product_catalog.services import variant_allows_purchase
        from management.services.ig_catalog_pricing import resolve_product_pricing
        from productcolors.models import ProductColorVariant

        rows = list(
            ProductColorVariant.objects.filter(product=product)
            .select_related("color")
            .order_by("order", "id")
        )
    except Exception:
        if strict:
            raise
        return []
    result = []
    options = dict(option_values or {})
    if fit_code:
        options["fit"] = fit_code
    for row in rows:
        name = str(getattr(getattr(row, "color", None), "name", "") or "").strip()
        try:
            allowed = variant_allows_purchase(
                product,
                row,
                fit_code=fit_code,
                size=size,
                option_values=options,
            )
        except Exception:
            # A failed option-rule lookup is not evidence that the variant is
            # sellable. Let the caller block checkout instead of guessing.
            raise
        if not allowed:
            continue
        pricing = {"display": "", "exact": False} if strict else resolve_product_pricing(
            product,
            variants=[row],
            selected_variant_id=row.pk,
            option_values=options,
        )
        result.append({
            "variant_id": row.pk,
            "name": name,
            "stock": int(getattr(row, "stock", 0) or 0),
            "price": pricing["display"],
            "price_exact": pricing["exact"],
        })
    return result


def _active_deal_state(client):
    try:
        from management.models import IgDeal
        from management.services.bot_payments import invoice_link_state

        deal = (
            IgDeal.objects.filter(client=client)
            .exclude(status=IgDeal.Status.CANCELLED)
            .order_by("-id")
            .first()
        )
    except Exception:
        return {"status": "none", "expires_at": None, "deal_id": None}
    if deal is None:
        return {"status": "none", "expires_at": None, "deal_id": None}
    try:
        state = invoice_link_state(deal)
    except Exception:
        state = {"status": "unknown", "expires_at": None}
    return {
        "status": state.get("status") or "unknown",
        "expires_at": state.get("expires_at"),
        "deal_id": deal.pk,
    }


def checkout_readiness(
    client,
    *,
    product_id=None,
    requested_size: str = "",
    requested_fit: str = "",
) -> dict:
    """Що вже відомо для замовлення і чого бракує, щоб створити посилання."""
    if not getattr(client, "pk", None):
        result = selection_readiness(product_id=None, selection={}, size="", fit="", quantity=1)
        result["missing"] = []
        return result
    selected_product_id = _int_or_none(product_id) or _int_or_none(getattr(client, "current_product_id", None))
    link = _active_deal_state(client)
    result = selection_readiness(
        product_id=selected_product_id,
        selection=_selection_state(client, selected_product_id),
        size=requested_size or getattr(client, "current_size", "") or "",
        fit=requested_fit,
        quantity=getattr(client, "current_qty", 1),
        color=getattr(client, "current_color", "") or "",
    )
    result["link"] = link
    return result


def selection_readiness(*, product_id, selection, size, fit="", quantity=1, color="", strict=False):
    """Catalog facts for explicit selection inputs; never reads a client or deal.

    ``strict`` exposes unresolved applicability for read-only UI projections.
    The legacy wrapper keeps its existing tolerant catalog behavior.
    """
    result = {
        "has_product": False,
        "product": None,
        "fit": {"required": False, "selected": "", "options": []},
        "size": {"required": False, "selected": "", "available": [], "disabled": [], "requested_unavailable": ""},
        "color": {"required": False, "selected": "", "selected_variant_id": None, "options": []},
        "options": {"required": False, "selected": {}, "missing": [], "axes": []},
        "quantity": 1,
        "missing": [],
        "can_issue_link": False,
        "link": {"status": "none", "expires_at": None},
    }
    product_id = _int_or_none(product_id)
    try:
        result["quantity"] = max(1, int(quantity or 1))
    except (TypeError, ValueError):
        result["quantity"] = 1
    if not product_id:
        result["missing"] = ["product"]
        return result

    try:
        from storefront.models import Product, ProductStatus

        product = Product.objects.filter(pk=product_id).select_related("category").first()
    except Exception:
        return result
    if product is None or product.status != ProductStatus.PUBLISHED:
        result["missing"] = ["product"]
        result["product"] = {
            "id": product_id,
            "title": getattr(product, "title", "") or "",
            "published": False,
        }
        return result

    result["has_product"] = True
    result["product"] = {
        "id": product.pk,
        "title": product.title,
        "price": "",
        "price_exact": False,
        "published": True,
        "slug": product.slug,
    }

    fit_rows = _fit_rows(product, **({"strict": True} if strict else {}))
    fit_selected = str(
        fit or selection.get("fit_option_code") or ""
    ).strip().lower()
    if fit_rows and fit_selected not in {str(row.code).lower() for row in fit_rows}:
        fit_selected = ""
    result["fit"] = {
        "required": bool(fit_rows),
        "selected": fit_selected,
        "options": [{"code": str(row.code).lower(), "label": row.label} for row in fit_rows],
    }
    if fit_rows and not fit_selected:
        result["missing"].append("fit")

    # Розміри вважаємо з урахуванням уже обраного кольору: правила розмірів
    # прив'язані до варіанта, тому порядок «колір → розмір» тут обов'язковий.
    preselected_variant = None
    preselected_variant_id = _int_or_none(selection.get("color_variant_id"))
    if preselected_variant_id:
        try:
            from productcolors.models import ProductColorVariant

            preselected_variant = (
                ProductColorVariant.objects.filter(
                    pk=preselected_variant_id, product=product
                )
                .select_related("color")
                .first()
            )
        except Exception:
            if strict:
                raise
            preselected_variant = None
    if preselected_variant is None:
        try:
            from productcolors.models import ProductColorVariant

            variants = list(ProductColorVariant.objects.filter(product=product)[:2])
            if len(variants) == 1:
                preselected_variant = variants[0]
        except Exception:
            if strict:
                raise
            preselected_variant = None

    grid = sizes_for_fit(product, fit_selected, variant=preselected_variant, **({"strict": True} if strict else {}))
    size_selected = str(
        size or ""
    ).strip().upper()
    requested_unavailable = ""
    if size_selected and grid["available"] and size_selected not in grid["available"]:
        requested_unavailable = size_selected
        size_selected = ""
    result["size"] = {
        "required": bool(grid["available"] or grid["disabled"]),
        "selected": size_selected,
        "available": grid["available"],
        "disabled": grid["disabled"],
        "requested_unavailable": requested_unavailable,
    }
    if result["size"]["required"] and not size_selected:
        result["missing"].append("size")

    selected_option_values = selection.get("option_values")
    selected_option_values = (
        {str(key).strip().lower(): str(value).strip().lower() for key, value in selected_option_values.items()
         if str(key).strip() and str(value).strip()}
        if isinstance(selected_option_values, dict) else {}
    )
    if fit_selected:
        selected_option_values["fit"] = fit_selected
    option_context_error = False
    try:
        colors = _color_rows(
            product,
            fit_code=fit_selected,
            size=size_selected,
            option_values=selected_option_values,
            **({"strict": True} if strict else {}),
        )
    except Exception:
        colors = []
        option_context_error = True
    selected_variant_id = _int_or_none(selection.get("color_variant_id"))
    selected_color_name = ""
    if selected_variant_id:
        match = next((row for row in colors if row["variant_id"] == selected_variant_id), None)
        if match is None:
            selected_variant_id = None
        else:
            selected_color_name = match["name"]
    if not selected_variant_id and len(colors) == 1:
        selected_variant_id = colors[0]["variant_id"]
        selected_color_name = colors[0]["name"]
    from management.services.ig_catalog_pricing import resolve_product_pricing
    try:
        from product_catalog.services import product_option_context
        from productcolors.models import ProductColorVariant

        option_variant = preselected_variant
        if option_variant is None and selected_variant_id:
            option_variant = (
                ProductColorVariant.objects.filter(
                    pk=selected_variant_id, product=product
                ).select_related("color").first()
            )
        option_context = product_option_context(
            product,
            variant=option_variant,
            option_values=selected_option_values,
        )
    except Exception:
        option_context = None
    option_axes = []
    option_missing = []
    option_selected = dict(selected_option_values)
    if option_context is None:
        option_context_error = True
        option_context = {"axes": []}
    known_option_axes = {
        str(axis.get("code") or "").strip().lower()
        for axis in option_context.get("axes") or []
        if str(axis.get("code") or "").strip()
    }
    for key in list(option_selected):
        if key != "fit" and key not in known_option_axes:
            option_missing.append(key)
    for axis in option_context.get("axes") or []:
        axis_code = str(axis.get("code") or "").strip().lower()
        if not axis_code or axis_code == "fit":
            continue
        choices = [
            {
                "code": str(choice.get("code") or "").strip().lower(),
                "label": str(choice.get("label") or choice.get("code") or ""),
                "is_enabled": bool(choice.get("is_enabled")),
                "is_default": bool(choice.get("is_default")),
                "price_delta": choice.get("option_price_delta", choice.get("price_delta", 0)),
            }
            for choice in axis.get("choices") or []
            if str(choice.get("code") or "").strip()
        ]
        enabled = [choice for choice in choices if choice["is_enabled"]]
        selected = option_selected.get(axis_code, "")
        if selected not in {choice["code"] for choice in enabled}:
            selected = ""
        fixed = axis.get("fixed_choice")
        if not selected and fixed:
            selected = str(fixed.get("code") or "").strip().lower()
        if not selected and len(enabled) == 1:
            selected = enabled[0]["code"]
        if selected:
            option_selected[axis_code] = selected
        elif not enabled or len(enabled) > 1:
            option_missing.append(axis_code)
        option_axes.append({
            "code": axis_code,
            "label": str(axis.get("label") or axis_code),
            "selected": selected,
            "choices": choices,
            "is_fixed": bool(fixed) or len(enabled) == 1,
        })
    result["options"] = {
        "required": bool(option_axes),
        "selected": option_selected,
        "missing": option_missing,
        "axes": option_axes,
    }
    if option_context_error:
        result["options"]["error"] = "unavailable"

    selected_pricing = (
        {"display": "", "exact": False}
        if option_context_error or strict
        else resolve_product_pricing(
            product,
            variants=[preselected_variant] if preselected_variant is not None else None,
            selected_variant_id=selected_variant_id,
            option_values=option_selected,
        )
    )
    if selected_pricing["display"]:
        result["product"]["price"] = (
            f"{selected_pricing['display']}.00"
            if selected_pricing["exact"] and "." not in selected_pricing["display"]
            else selected_pricing["display"]
        )
        result["product"]["price_exact"] = selected_pricing["exact"]
    result["color"] = {
        "required": len(colors) > 1,
        "selected": selected_color_name or str(color or ""),
        "selected_variant_id": selected_variant_id,
        "options": colors,
    }
    if len(colors) > 1 and not selected_variant_id:
        result["missing"].append("color")

    if option_context_error:
        result["missing"].append("options_unavailable")
    result["missing"].extend(f"option:{code}" for code in option_missing)
    result["can_issue_link"] = not result["missing"]
    if strict:
        # Empty filtered colors cannot prove there is no color axis. A missing
        # fit/color can also change the applicable size grid: abstain then.
        result["applicability_known"] = bool(
            grid.get("resolved") and not option_context_error and colors
            and (not fit_rows or fit_selected)
            and (len(colors) == 1 or selected_variant_id)
            and (not preselected_variant_id or (
                getattr(preselected_variant, "pk", None) == preselected_variant_id
                and selected_variant_id == preselected_variant_id))
            and (not size_selected or size_selected in grid["available"])
        )
    return result


def _format_expiry(value) -> str:
    if not value:
        return ""
    try:
        from django.utils import timezone

        localized = timezone.localtime(value)
        return localized.strftime("%H:%M %d.%m")
    except Exception:
        return ""


def readiness_prompt_note(client, *, readiness: dict | None = None) -> str:
    """Службовий блок для system_instruction. Клієнт цього тексту не бачить.

    Це навмисно опис **стану**, а не готова реплика. Раніше формулювання питання
    жило в коді, і тому воно приходило клієнту дослівно однаковим, чужою мовою і
    без зв'язку з тим, що він щойно написав.
    """
    if not getattr(client, "pk", None):
        return ""
    state = readiness if isinstance(readiness, dict) else checkout_readiness(client)
    if not state.get("has_product") and "product" not in state.get("missing", []):
        return ""

    lines = ["[СТАН ОФОРМЛЕННЯ — службове, порахований з каталогу й БД; клієнту не переказуй дослівно]"]
    product = state.get("product") or {}
    if product.get("published"):
        lines.append(f"товар: {product.get('title')} (id={product.get('id')})")
        if product.get("price") and product.get("price_exact"):
            lines.append(f"точна ціна конфігурації: {product.get('price')} грн")
        elif product.get("price"):
            lines.append(
                f"діапазон цін конфігурацій: {product.get('price')} грн; "
                "точну ціну не називай, доки не обрані колір/матеріал і фасон/опції"
            )
        else:
            lines.append(
                "ціна конфігурації не визначена; не підставляй базову ціну товару, "
                "уточни колір/матеріал і фасон/опції або передай менеджеру"
            )
    elif product.get("id"):
        lines.append(
            f"товар id={product.get('id')} більше не опублікований — не обіцяй його, "
            "запропонуй разом підібрати інший із каталогу"
        )
    else:
        lines.append("товар: ще не визначено")

    fit = state.get("fit") or {}
    if fit.get("required"):
        options = ", ".join(option["code"] for option in fit.get("options") or [])
        if fit.get("selected"):
            lines.append(f"фасон: {fit['selected']} (обрано)")
        else:
            lines.append(f"фасон: не обрано; доступні: {options}")

    option_state = state.get("options") or {}
    for axis in option_state.get("axes") or []:
        choices = ", ".join(
            f"{choice['code']} ({choice['label']})"
            + (f" +{choice['price_delta']} грн" if choice.get("price_delta") else "")
            for choice in axis.get("choices") or []
            if choice.get("is_enabled")
        )
        selected = axis.get("selected") or ""
        if selected:
            lines.append(f"опція {axis.get('label') or axis.get('code')}: {selected} (обрано)")
        elif choices:
            lines.append(
                f"опція {axis.get('label') or axis.get('code')}: не обрано; доступні: {choices}"
            )

    size = state.get("size") or {}
    if size.get("required"):
        available = ", ".join(size.get("available") or []) or "невідомо"
        if size.get("selected"):
            lines.append(f"розмір: {size['selected']} (обрано); доступні: {available}")
        else:
            lines.append(f"розмір: не обрано; доступні: {available}")
        if size.get("requested_unavailable"):
            lines.append(
                f"УВАГА: розмір {size['requested_unavailable']} зараз недоступний. "
                "Скажи це чесно й своїми словами: ти перевірила наявність, цього розміру "
                "немає, ти уточниш у менеджера, чи можна щось зробити, і менеджер "
                "повернеться з відповіддю — а поки можна обрати розмір із доступних. "
                "Не вигадуй, що розмір є."
            )
        if size.get("disabled"):
            lines.append(f"вимкнені розміри (немає в наявності): {', '.join(size['disabled'])}")

    color = state.get("color") or {}
    if color.get("required") or color.get("selected"):
        options = ", ".join(
            f"{option['name']} (variant_id={option['variant_id']}, "
            f"ціна {option.get('price') or 'уточнюється'} грн)"
            for option in color.get("options") or []
            if option.get("name")
        )
        if color.get("selected_variant_id"):
            lines.append(
                f"колір: {color.get('selected') or 'обрано'} "
                f"(variant_id={color['selected_variant_id']})"
            )
        elif options:
            lines.append(f"колір: не обрано; доступні: {options}")

    lines.append(f"кількість: {state.get('quantity') or 1}")

    link = state.get("link") or {}
    link_status = link.get("status") or "none"
    if link_status == "live":
        expiry = _format_expiry(link.get("expires_at"))
        lines.append(
            "діюче посилання на оплату: є" + (f", дійсне до {expiry}" if expiry else "")
        )
    elif link_status == "expired":
        lines.append("попереднє посилання на оплату вже недійсне — можна створити нове")
    elif link_status == "paid":
        lines.append("це замовлення вже оплачене — не пропонуй оплату повторно")
    elif link_status == "unknown":
        lines.append(
            "попереднє посилання є, але термін його дії невідомий — не стверджуй, "
            "що воно активне; якщо клієнт просить, створи нове"
        )
    else:
        lines.append("діючого посилання на оплату немає")

    missing = state.get("missing") or []
    if state.get("can_issue_link"):
        lines.append(
            "посилання на оплату: усі дані відомі. Якщо клієнт підтвердив покупку — "
            "постав у кінці відповіді [PAYLINK:full] [PRODUCT:<id>] і "
            "[ITEM:<product_id>|<qty>|<size>|<fit>|<color_variant_id>]."
        )
    else:
        human = {
            "product": "товар",
            "fit": "фасон",
            "size": "розмір",
            "color": "колір",
        }
        lacking = ", ".join(human.get(item, item) for item in missing)
        lines.append(
            f"посилання на оплату: ще не можна створити, бракує: {lacking}. "
            "Запитай лише те, чого бракує — по одному питанню, своїми словами, "
            "мовою клієнта, з опорою на те, що він щойно написав. Не повторюй "
            "дослівно своє попереднє питання: якщо клієнт не відповів на нього, "
            "переформулюй або спитай інакше. Тег [PAYLINK] поки не став."
        )
    return "\n".join(lines)
