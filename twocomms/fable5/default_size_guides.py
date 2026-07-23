"""Canonical fit-specific guides used by the storefront and backfill command."""

CLASSIC_GUIDE_DATA = {
    "profile_key": "classic_tshirt_fs101",
    "title": "Таблиця розмірів класичної футболки",
    "eyebrow": "Standart CRC FS-101",
    "intro": (
        "Фактичні виміри чоловічої базової футболки у розкладеному вигляді. "
        "Допустима похибка ручного вимірювання ±1–2 см."
    ),
    "columns": [
        {"key": "size", "label": "Міжнародний розмір"},
        {"key": "chest", "label": "Обхват грудей"},
        {"key": "garment_length", "label": "Довжина виробу"},
        {"key": "sleeve_length", "label": "Довжина рукава"},
        {"key": "shoulder_width", "label": "Ширина плечей"},
    ],
    "rows": [
        {"size": "S", "chest": "92", "garment_length": "65", "sleeve_length": "16", "shoulder_width": "43"},
        {"size": "M", "chest": "100", "garment_length": "68", "sleeve_length": "17", "shoulder_width": "44"},
        {"size": "L", "chest": "108", "garment_length": "70", "sleeve_length": "19", "shoulder_width": "47"},
        {"size": "XL", "chest": "116", "garment_length": "74", "sleeve_length": "21", "shoulder_width": "49"},
        {"size": "2XL", "chest": "124", "garment_length": "76", "sleeve_length": "22", "shoulder_width": "52"},
        {"size": "3XL", "chest": "132", "garment_length": "79", "sleeve_length": "24", "shoulder_width": "53"},
    ],
    "notes": [
        "Заміри вказані в сантиметрах і зняті з готового виробу.",
        "3XL наведений у таблиці виробника; доступність розміру перевіряйте у картці товару.",
    ],
}

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
