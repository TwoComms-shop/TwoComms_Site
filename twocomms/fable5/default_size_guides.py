"""Canonical fit-specific guides used by the storefront and backfill command."""

OVERSIZE_GUIDE_DATA = {
    "profile_key": "oversize_tshirt",
    "title": "Таблиця розмірів оверсайз футболки",
    "eyebrow": "Oversize T-shirt",
    "intro": (
        "Фактичні виміри виробу у розкладеному вигляді. "
        "Допустима похибка ручного вимірювання ±1–2 см."
    ),
    "columns": [
        {"key": "size", "label": "Міжнародний розмір"},
        {"key": "garment_length", "label": "Довжина виробу"},
        {"key": "shoulder_length", "label": "Довжина плеча"},
        {"key": "sleeve_length", "label": "Довжина рукава"},
        {"key": "chest", "label": "Обхват грудей"},
        {"key": "shoulder_width", "label": "Ширина плечей"},
    ],
    "rows": [
        {"size": "XS", "garment_length": "70", "shoulder_length": "14", "sleeve_length": "25", "chest": "102", "shoulder_width": "42"},
        {"size": "S", "garment_length": "70", "shoulder_length": "14", "sleeve_length": "25", "chest": "108", "shoulder_width": "45"},
        {"size": "M", "garment_length": "70", "shoulder_length": "14", "sleeve_length": "25", "chest": "110", "shoulder_width": "45"},
        {"size": "L", "garment_length": "70", "shoulder_length": "14", "sleeve_length": "25", "chest": "115", "shoulder_width": "46"},
        {"size": "XL", "garment_length": "70", "shoulder_length": "14", "sleeve_length": "25", "chest": "117", "shoulder_width": "46"},
        {"size": "2XL", "garment_length": "71", "shoulder_length": "14", "sleeve_length": "25", "chest": "124", "shoulder_width": "47"},
    ],
    "notes": [
        "Оверсайз має вільнішу посадку та починається з XS.",
        "Знімайте мірки з футболки, розкладеної на рівній поверхні.",
    ],
}
