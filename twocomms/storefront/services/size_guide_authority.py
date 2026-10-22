"""Pure recognition of the historical generated hoodie chart.

The old seed command persisted these defaults without a measured source.
Recognizing that exact data/provenance signature does not authorize new
measurements or change catalog size availability.
"""

from decimal import Decimal, InvalidOperation


def is_unverified_legacy_hoodie_seed(guide_data):
    """Identify the known seed, including copies persisted in ``guide_data``.

    Titles/notes may have been edited independently; the original preset
    provenance markers plus exact size/cell signature remain unverified. No
    new boolean review flag can silently bypass this source check.
    """
    if not isinstance(guide_data, dict) or guide_data.get("profile_key") != "hoodie":
        return False
    aliases = guide_data.get("aliases")
    if (guide_data.get("eyebrow") != "Hoodie fit guide" or not isinstance(aliases, list)
        or not all(isinstance(item, str) for item in aliases)
        or set(aliases) != {"hoodie", "hoodies", "худі", "худи", "fleece", "флис"}):
        return False
    columns, rows = guide_data.get("columns"), guide_data.get("rows")
    if (not isinstance(columns, list) or not all(isinstance(item, dict) for item in columns)
        or [item.get("key") for item in columns] != ["size", "length", "width"]
        or not isinstance(rows, list) or len(rows) != 6):
        return False
    actual = {}
    try:
        for row in rows:
            if not isinstance(row, dict):
                return False
            size = str(row.get("size", "")).strip().upper()
            size = "XXL" if size in {"2XL", "X2L"} else size
            cells = tuple(Decimal(str(row[key]).replace(",", ".")) for key in ("length", "width"))
            if not all(value.is_finite() for value in cells):
                return False
            actual[size] = cells
    except (InvalidOperation, KeyError, TypeError, ValueError):
        return False
    return actual == {
        "XS": (Decimal(54), Decimal(52)), "S": (Decimal(58), Decimal(56)),
        "M": (Decimal(67), Decimal(60)), "L": (Decimal(70), Decimal(64)),
        "XL": (Decimal(73), Decimal(68)), "XXL": (Decimal(78), Decimal(72)),
    }
