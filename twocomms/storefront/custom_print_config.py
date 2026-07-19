from __future__ import annotations

from copy import deepcopy

from django.utils.translation import gettext_lazy as _


TELEGRAM_MANAGER_URL = "https://t.me/twocomms"
CUSTOM_PRINT_DRAFT_STORAGE_KEY = "twocomms.custom_print.v2.draft"
SESSION_CUSTOM_CART_KEY = "custom_print_cart"

# ── V2 business constants ────────────────────────────────────────────
GIFT_SERVICE = {
    "value": "gift_pack",
    "label": _("Подарункова упаковка"),
    "price": 100,
    "promo_code": "GIFT10",
    "promo_discount_percent": 10,
    "note": _("Ми упакуємо замовлення в преміум-крафт, додамо листівку і заховаємо цінники."),
    "bonus_note": _("Бонус: разовий промокод -10% на наступну покупку в TwoComms."),
}

B2B_TIER = {
    "unit_step": 5,
    "discount_per_unit": 10,
    "hint": _("Кожні 5 одиниць — -10 грн/шт. Калькулятор оновлюється миттєво."),
}

SIZE_GRID = ["XS", "S", "M", "L", "XL", "2XL"]

PROGRESS_STEPS = [
    {"value": "format", "label": _("Формат")},
    {"value": "garment", "label": _("Виріб")},
    {"value": "config", "label": _("Налаштування")},
    {"value": "placement", "label": _("Розташування")},
    {"value": "artwork", "label": _("Макет")},
    {"value": "quantity", "label": _("Кількість")},
    {"value": "gift", "label": _("Подарунок")},
    {"value": "contact", "label": _("Контакт і перевірка")},
]

# Runtime validation copy belongs in the server configuration so the browser
# does not become a second, untranslatable source of customer-facing text.
UI_STRINGS = {
    "mode_required": _("Оберіть формат замовлення."),
    "product_required": _("Оберіть виріб."),
    "product_config_required": _("Завершіть налаштування виробу."),
    "config_fit_required": _("Оберіть посадку."),
    "config_fabric_required": _("Оберіть тканину."),
    "config_color_required": _("Оберіть колір."),
    "placement_required": _("Оберіть і налаштуйте зони друку."),
    "artwork_service_required": _("Оберіть сценарій роботи з макетом."),
    "artwork_brief_design_required": _("Опишіть бриф / завдання для дизайну."),
    "artwork_brief_adjust_required": _("Опишіть, що саме потрібно змінити у файлі."),
    "artwork_file_required": _("Додайте макет для кожної вибраної зони."),
    "quantity_required": _("Заповніть кількість і розміри."),
    "contact_required": _("Заповніть ім'я, канал зв'язку і контакт."),
    "thermo_fabric": _("Термохромна тканина"),
    "gift_continue_off": _("Продовжити без подарункової упаковки"),
    "gift_continue_on": _("Продовжити з подарунковою упаковкою"),
    "contact_channel_hint": _("Оберіть один зручний канал — менеджер відповість саме туди."),
    "manager_greeting": _("Привіт! Хочу обговорити кастомний принт TwoComms."),
    "fleece_title": _("Утеплення"),
    "fleece_on": _("З флісом"),
    "fleece_off": _("Без флісу"),
}

FRONT_SIZE_PRESETS = [
    {"value": "A6", "label": "A6", "stage_scale": 0.44, "price_delta": 40, "range_label": _("до 10,5 × 14,8 см")},
    {"value": "A5", "label": "A5", "stage_scale": 0.58, "price_delta": 50, "range_label": _("до 14,8 × 21 см")},
    {"value": "A4", "label": "A4", "stage_scale": 0.74, "price_delta": 60, "range_label": _("до 21 × 29,7 см")},
]
FRONT_SIZE_DEFAULT = "A4"

BACK_SIZE_PRESETS = [
    {"value": "A4", "label": "A4", "stage_scale": 0.62, "price_delta": 60, "range_label": _("до 21 × 29,7 см")},
    {"value": "A3", "label": "A3", "stage_scale": 0.78, "price_delta": 80, "range_label": _("до 29,7 × 42 см")},
    {"value": "A2", "label": "A2", "stage_scale": 0.92, "price_delta": 110, "range_label": _("до 42 × 59,4 см")},
]
BACK_SIZE_DEFAULT = "A4"

SLEEVE_MODE_OPTIONS = [
    {"value": "a6", "label": "A6", "badge": "A6 · +40 грн", "price_delta": 40, "stage_scale": 0.42},
    {"value": "full_text", "label": _("На весь рукав текстом"), "badge": _("Текст · +60 грн"), "price_delta": 60, "stage_scale": 0.94},
]
SLEEVE_MODE_DEFAULT = "a6"

STAGE_META = {
    "placeholder_title": _("Виріб на сцені"),
    "placeholder_note": _("Оберіть виріб, щоб побачити сцену, зони і масштаб принта."),
}

# ── Display labels (legacy + V2) ─────────────────────────────────────
ZONE_LABELS = {
    "front": _("Спереду"),
    "back": _("На спині"),
    "kangaroo": _("Кенгуряча кишеня"),
    "sleeve": _("На рукавах"),
    "sleeve_left": _("Лівий рукав"),
    "sleeve_right": _("Правий рукав"),
    "custom": _("Інша зона"),
}

PRODUCT_LABELS = {
    "hoodie": _("Худі"),
    "tshirt": _("Футболка"),
    "longsleeve": _("Лонгслів"),
    "customer_garment": _("Свій одяг"),
}

FIT_LABELS = {
    "regular": _("Класичний"),
    "oversize": _("Оверсайз"),
}

FABRIC_LABELS = {
    "standard": _("База"),
    "premium": _("Преміум"),
    "thermo": _("Термо"),
}

SERVICE_LABELS = {
    "ready": _("Готовий файл"),
    "adjust": _("Потрібно допрацювати"),
    "design": _("Потрібен дизайн"),
}

TRIAGE_LABELS = {
    "print-ready": "Готовий до друку",
    "needs-work": "Потрібна підготовка",
    "reference-only": "Лише референс",
    "needs-review": "Потрібна перевірка",
}

ADDON_LABELS = {
    "lacing": "Люверси зі шнурками",
    "grommets": "Люверси зі шнурками",
    "inside_label": "Люверси зі шнурками",
    "hem_tag": "Люверси зі шнурками",
    "fleece": "З флісом",
    "no_fleece": "Без флісу",
    "ribbed_neck": "Щільна горловина (Рібана)",
    "twill_tape": "Кіперна стрічка",
}

# Legacy (kept for back-compat with old drafts / admin filters).
QUICK_START_MODES = [
    {
        "value": "start_blank",
        "label": "Почати з нуля",
        "hint": "Збираємо конфігурацію покроково: виріб, зони, стиль і контакт.",
    },
    {
        "value": "have_file",
        "label": "У мене є файл",
        "hint": "Фокус на перевірці макета, оцінці файлів і швидкому запуску в роботу.",
    },
    {
        "value": "starter_style",
        "label": "Показати стартові стилі",
        "hint": "Починаємо з кураторського напряму і швидко збираємо зрозумілий бриф.",
    },
]

CLIENT_MODES = [
    {
        "value": "personal",
        "label": _("Для себе"),
        "hint": _("Один виріб або невелика серія без зайвої бюрократії."),
        "icon": "user",
    },
    {
        "value": "brand",
        "label": _("Для команди / бренду"),
        "hint": _("Від 5 штук — знижка рахується автоматично. Велику партію менеджер прорахує індивідуально."),
        "icon": "brand",
    },
]

STARTER_STYLES = [
    {
        "value": "minimal",
        "label": _("Мінімальний"),
        "accent": _("Чисті пропорції, один акцент, багато повітря."),
    },
    {
        "value": "bold",
        "label": _("Сміливий"),
        "accent": _("Контрастні площини, великі композиції, більш помітний жест."),
    },
    {
        "value": "logo-first",
        "label": _("Лого в центрі"),
        "accent": _("Логотип або короткий знак як центр всієї композиції."),
    },
]

ARTWORK_SERVICES = [
    {
        "value": "ready",
        "label": _("Готовий файл"),
        "price_delta": 0,
        "hint": _("Прозорий PNG або чистий вектор, уже готовий до друку. Менеджер однаково перевірить файл."),
        "badge": "0 грн",
    },
    {
        "value": "adjust",
        "label": _("Потрібно допрацювати"),
        "price_delta": 100,
        "hint": _("Можемо почистити чи адаптувати файл: приберемо просте чорне або біле тло, очистимо напівпрозорі пікселі чи внесемо невелику технічну зміну."),
        "badge": "+100 грн",
    },
    {
        "value": "design",
        "label": _("Потрібен дизайн"),
        "price_delta": 300,
        "hint": _("Відтворимо референс або створимо дизайн з нуля за вашою ідеєю та побажаннями."),
        "badge": "+300 грн",
    },
]

TRIAGE_STATUSES = [
    {
        "value": "print-ready",
        "label": _("Готовий до друку"),
        "hint": _("Файл виглядає готовим до друку без додаткової підготовки."),
    },
    {
        "value": "needs-work",
        "label": _("Потрібна підготовка"),
        "hint": _("Потрібно підчистити, перевести у правильний формат або підготувати деталі."),
    },
    {
        "value": "reference-only",
        "label": _("Лише референс"),
        "hint": _("Це референс, на основі якого ще треба зібрати робочий макет."),
    },
]

SIZE_MODES = [
    {"value": "single", "label": _("Один розмір")},
    {"value": "mixed", "label": _("Мікс розмірів")},
    {"value": "manager", "label": _("Уточню з менеджером")},
]

CONTACT_CHANNELS = [
    {
        "value": "telegram",
        "label": "Telegram",
        "placeholder": "@username або https://t.me/username",
    },
    {
        "value": "whatsapp",
        "label": "WhatsApp",
        "placeholder": "+380...",
    },
    {
        "value": "phone",
        "label": _("Телефон"),
        "placeholder": "+380...",
    },
]


def _svg_markup(*lines: str) -> str:
    return "\n".join(line.strip() for line in lines if line is not None).strip()


def _stage_box(x: float, y: float, width: float, height: float, rotate: float = 0, radius: float = 18, shape: str = "panel") -> dict:
    return {
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "rotate": rotate,
        "radius": radius,
        "shape": shape,
    }

ISO_SIZES = {
    "A6": (105, 148),
    "A5": (148, 210),
    "A4": (210, 297),
    "A3": (297, 420),
    "A2": (420, 594),
}

FORMAT_DIMENSIONS = {
    key: {"width_mm": width_mm, "height_mm": height_mm}
    for key, (width_mm, height_mm) in ISO_SIZES.items()
}

PREVIEW_ASSETS = {
    "hoodie:regular": {
        "front": "/static/img/configurator/studio/hoodie-regular-front.png",
        "back": "/static/img/configurator/studio/hoodie-regular-back.png",
        "lacing": "/static/img/configurator/studio/hoodie-lacing.png",
    },
    "hoodie:oversize": {
        "front": "/static/img/configurator/studio/hoodie-oversize-front.png",
        "back": "/static/img/configurator/studio/hoodie-oversize-back.png",
        "lacing": "/static/img/configurator/studio/hoodie-lacing.png",
    },
    "tshirt:regular": {
        "front": "/static/img/configurator/studio/tshirt-regular-front.png",
        "back": "/static/img/configurator/studio/tshirt-regular-back.png",
    },
    "tshirt:oversize": {
        "front": "/static/img/configurator/studio/tshirt-oversize-front.png",
        "back": "/static/img/configurator/studio/tshirt-oversize-back.png",
    },
    "longsleeve:regular": {
        "front": "/static/img/configurator/studio/longsleeve-front.png",
        "back": "/static/img/configurator/studio/longsleeve-back.png",
    },
}


def _preview_calibration(garment_width_mm: int, allowed_zones: list[str]) -> dict:
    return {
        "canvas": {"width": 1200, "height": 1400},
        "garment_width_mm": garment_width_mm,
        "allowed_zones": allowed_zones,
        "zones": {
            "body": {"x": 25.0, "y": 16.0, "width": 50.0, "height": 72.0},
            "front": {"x": 50.0, "y": 43.0},
            "back": {"x": 50.0, "y": 44.0},
            "sleeve_left": {"x": 17.0, "y": 48.0, "rotate": 12},
            "sleeve_right": {"x": 83.0, "y": 48.0, "rotate": -12},
        },
    }


PREVIEW_CALIBRATION = {
    "hoodie:regular": _preview_calibration(600, ["front", "back", "kangaroo", "sleeve"]),
    "hoodie:oversize": _preview_calibration(650, ["front", "back", "kangaroo", "sleeve"]),
    "tshirt:regular": _preview_calibration(520, ["front", "back"]),
    "tshirt:oversize": _preview_calibration(600, ["front", "back"]),
    "longsleeve:regular": _preview_calibration(540, ["front", "back", "sleeve"]),
}

def calc_iso_box(format_key: str, body_width_mm: float, svg_body_width: float, svg_collar_y: float, top_offset_mm: float = 50, x_center: float = 50, radius: float = 24, shape: str = "panel", padding_mm: float = 0) -> dict:
    w_mm, h_mm = ISO_SIZES.get(format_key, (210, 297))
    scale = svg_body_width / body_width_mm
    
    box_w = (w_mm + padding_mm) * scale
    box_h = (h_mm + padding_mm) * scale
    
    y_start_svg = svg_collar_y + (top_offset_mm * scale)
    y_center_svg = y_start_svg + (box_h / 2.0)
    
    width_pct = (box_w / 420.0) * 100
    height_pct = (box_h / 520.0) * 100
    y_center_pct = (y_center_svg / 520.0) * 100
    
    return _stage_box(x_center, round(y_center_pct, 1), round(width_pct, 1), round(height_pct, 1), 0, radius, shape)



def _stage_anchor(button_x: float, button_y: float, *, presets: dict | None = None, modes: dict | None = None, default: dict | None = None) -> dict:
    payload = {
        "button": {
            "x": button_x,
            "y": button_y,
        },
    }
    if presets:
        payload["presets"] = presets
    if modes:
        payload["modes"] = modes
    if default:
        payload["default"] = default
    return payload

# ── Product matrix ───────────────────────────────────────────────────
PRODUCT_MATRIX = {
    "hoodie": {
        "label": _("Худі"),
        "eyebrow": _("Головний сценарій"),
        "summary": _("Максимум налаштувань: тканина, посадка, колір, зони й деталі."),
        "hero_note": _("Найзручніший старт, якщо хочете точно зібрати худі під свій принт."),
        "fits": [
            {"value": "regular", "label": _("Класичний"), "description": _("Базова посадка для щоденного мерчу.")},
            {"value": "oversize", "label": _("Оверсайз"), "description": _("Більш масивний силует з відчуттям преміум-речі.")},
        ],
        "fabrics": {
            "regular": [
                {"value": "standard", "label": _("Класика"), "short_desc": _("Базова тканина без пеньє-обробки."), "price_delta": 0, "included_in_base": True},
                {
                    "value": "premium",
                    "label": _("Преміум"),
                    "short_desc": _("Пеньє-обробка та щільніші шви."),
                    "price_delta": 250,
                    "included_in_base": False,
                    "info_title": _("Що таке преміум для худі?"),
                    "info_desc": (
                        _("Преміум-варіант має вищу щільність, акуратнішу обробку пеньє та краще тримає форму навіть після активного носіння.\n"
                          "Полотно більш стійке до навантаження, зберігає гладку поверхню навіть після тривалого носіння та постійного тертя об сумку чи рюкзак, виглядає структурніше й дає відчутно чистішу основу під кастомний принт.")
                    ),
                    "info_theme": "premium",
                },
            ],
            "oversize": [
                {
                    "value": "premium",
                    "label": _("Преміум"),
                    "short_desc": _("Преміальна тканина з пеньє-обробкою."),
                    "price_delta": 0,
                    "included_in_base": True,
                    "info_title": _("Що таке преміум для худі?"),
                    "info_desc": (
                        _("Преміум-варіант має вищу щільність, акуратнішу обробку пеньє та краще тримає форму навіть після активного носіння.\n"
                          "Полотно більш стійке до навантаження, зберігає гладку поверхню навіть після тривалого носіння та постійного тертя об сумку чи рюкзак, виглядає структурніше й дає відчутно чистішу основу під кастомний принт.")
                    ),
                    "info_theme": "premium",
                },
            ],
            "zip_hoodie": [
                {"value": "standard", "label": _("Стандарт"), "price_delta": 0, "included_in_base": True},
                {
                    "value": "premium",
                    "label": _("Преміум"),
                    "price_delta": 250,
                    "included_in_base": False,
                    "info_title": _("Що таке преміум для худі?"),
                    "info_desc": (
                        _("Преміум-варіант має вищу щільність, акуратнішу обробку пеньє та краще тримає форму навіть після активного носіння.\n"
                          "Полотно більш стійке до навантаження, зберігає гладку поверхню навіть після тривалого носіння та постійного тертя об сумку чи рюкзак, виглядає структурніше й дає відчутно чистішу основу під кастомний принт.")
                    ),
                    "info_theme": "premium",
                },
            ],
        },
        "default_fit": "regular",
        "default_fabric": "standard",
        "colors": [
            {"value": "black", "label": _("Чорний"), "hex": "#151515"},
            {"value": "graphite", "label": _("Графіт"), "hex": "#3b3b3f"},
            {"value": "sand", "label": _("Пісочний"), "hex": "#c8b28d"},
            {"value": "bone", "label": _("Світлий"), "hex": "#ebe3d6"},
        ],
        "fit_colors": {
            "regular": [
                {"value": "black", "label": _("Чорний"), "hex": "#151515"},
                {"value": "graphite", "label": _("Графіт"), "hex": "#3b3b3f"},
                {"value": "sand", "label": _("Пісочний"), "hex": "#c8b28d"},
                {"value": "bone", "label": _("Світлий"), "hex": "#ebe3d6"},
            ],
            "oversize": [
                {"value": "black", "label": _("Чорний"), "hex": "#151515"},
                {"value": "pink", "label": _("Рожевий"), "hex": "#d98fa8"},
            ],
        },
        "default_color": "black",
        "zones": ["front", "back", "kangaroo", "sleeve"],
        "default_zones": [],
        "add_ons": [
            {
                "value": "lacing",
                "label": _("Люверси зі шнурками"),
                "price_delta": 150,
                "icon": "lacing",
                "badge": _("+150 грн"),
                "hint": _("Преміум-апгрейд: металеві люверси й унікальні шнурки замість стандартних."),
            },
            {
                "value": "fleece",
                "label": _("З флісом"),
                "price_delta": 0,
                "icon": "fleece",
                "group": "fleece"
            },
            {
                "value": "no_fleece",
                "label": _("Без флісу"),
                "price_delta": 0,
                "icon": "no_fleece",
                "group": "fleece"
            },
        ],
        "pricing": {
            "base": 1600,
            "premium_delta": 250,
            "oversize_delta": 200,
            "extra_zone_delta": 180,
            "add_on_delta": 0,  # V2: add-on prices are per-item via price_delta
        },
    },
    "tshirt": {
        "label": _("Футболка"),
        "eyebrow": _("Швидкий старт"),
        "summary": _("Швидкий старт для принта, дропа або подарунка."),
        "hero_note": _("Швидкий варіант, якщо потрібна футболка з фронтом, спиною або принтом на рукаві."),
        "fits": [
            {"value": "regular", "label": _("Класична"), "description": _("Рівна класична посадка для базового принта.")},
            {"value": "oversize", "label": _("Оверсайз"), "description": _("Більш вільний силует без додаткової анкети.")},
        ],
        "fabrics": {
            "regular": [
                {"value": "standard", "label": _("Класика"), "short_desc": _("Поки недоступна для цього сценарію."), "price_delta": 0, "included_in_base": False, "available": False, "disabled": True},
                {"value": "premium", "label": _("Преміум"), "short_desc": _("Турецький кулір, пеньє, покращені шви та ребана."), "price_delta": 0, "included_in_base": True},
                {"value": "thermo", "label": _("Термохромна тканина"), "short_desc": _("Змінює відтінок від тепла тіла."), "price_delta": 500, "included_in_base": False, "info_title": _("Термохромна тканина"), "info_desc": _("Дуже хороша якість, щільні шви та ребана. Від тепла тканина змінює відтінок."), "preview_image": "/static/img/configurator/ui/thermo-preview.png", "colors": [{"value": "thermo_green", "label": _("Зелений (Термо)"), "hex": "#8ba38d"}, {"value": "thermo_pink", "label": _("Рожевий (Термо)"), "hex": "#e78ba7"}]},
            ],
            "oversize": [
                {"value": "standard", "label": _("Класика"), "short_desc": _("Поки недоступна для цієї посадки."), "price_delta": 0, "included_in_base": False, "available": False, "disabled": True},
                {
                    "value": "premium",
                    "label": _("Преміум"),
                    "short_desc": _("Входить у базу оверсайзу: щільніша тканина, пеньє, покращені шви та ребана."),
                    "price_delta": 0,
                    "included_in_base": True,
                    "info_title": _("Преміум у базі оверсайзу"),
                    "info_desc": _("Оверсайз одразу шиється з преміум-тканини. Класична тканина для цієї посадки недоступна, щоб зберегти потрібну форму та щільність."),
                },
                {
                    "value": "thermo", "label": _("Термохромна тканина"), "price_delta": 500, "included_in_base": False,
                    "short_desc": _("Змінює відтінок від тепла тіла."),
                    "info_title": _("Термохромна тканина"),
                    "info_desc": _("Дуже хороша якість, щільні шви та ребана. Від тепла тканина змінює відтінок — це помітний, але стриманий ефект."),
                    "preview_image": "/static/img/configurator/ui/thermo-preview.png",
                    "colors": [
                        {"value": "thermo_green", "label": _("Зелений (Термо)"), "hex": "#8ba38d"},
                        {"value": "thermo_pink", "label": _("Рожевий (Термо)"), "hex": "#e78ba7"}
                    ]
                },
            ],
        },
        "default_fit": "regular",
        "default_fabric": "premium",
        "colors": [
            {"value": "black", "label": _("Чорний"), "hex": "#151515"},
            {"value": "white", "label": _("Білий"), "hex": "#f1ede6"},
            {"value": "coyote", "label": _("Койот"), "hex": "#8B6B45"},
        ],
        "default_color": "black",
        "zones": ["front", "back"],
        "default_zones": [],
                "add_ons": [
            {
                "value": "ribbed_neck",
                "label": _("Щільна горловина (Рібана)"),
                "price_delta": 0,
                "icon": "ribbed_neck",
                "badge": _("Включено"),
                "hint": _("Еластична горловина, що довго не втрачає форму."),
                "auto_include_condition": "premium_or_oversize"
            },
            {
                "value": "twill_tape",
                "label": _("Кіперна стрічка"),
                "price_delta": 0,
                "icon": "twill_tape",
                "badge": _("Включено"),
                "hint": _("Укріплення задньої частини шиї, підвищений комфорт."),
                "auto_include_condition": "premium_or_oversize"
            }
        ],
        "pricing": {
            "base": 700,
            "premium_delta": 0,
            "thermo_delta": 500,
            "oversize_delta": 200,
            "extra_zone_delta": 150,
            "add_on_delta": 0,
        },
    },
    "longsleeve": {
        "label": _("Лонгслів"),
        "eyebrow": _("Швидкий старт"),
        "summary": _("База між футболкою й худі — легше, але з характером."),
        "hero_note": _("Підійде, якщо потрібен чистий фронт, спина або акцент на рукаві."),
        "fits": [],
        "fabrics": {},
        "default_fit": "",
        "default_fabric": "",
        "colors": [
            {"value": "black", "label": _("Чорний"), "hex": "#151515"},
            {"value": "bone", "label": _("Світлий"), "hex": "#e7ddcf"},
            {"value": "olive", "label": _("Оливковий"), "hex": "#59604a"},
        ],
        "default_color": "black",
        "zones": ["front", "back", "sleeve"],
        "default_zones": [],
                "add_ons": [
            {
                "value": "ribbed_neck",
                "label": _("Щільна горловина (Рібана)"),
                "price_delta": 0,
                "icon": "ribbed_neck",
                "badge": _("Включено"),
                "hint": _("Еластична горловина, що довго не втрачає форму."),
                "auto_include_condition": "premium_or_oversize"
            },
            {
                "value": "twill_tape",
                "label": _("Кіперна стрічка"),
                "price_delta": 0,
                "icon": "twill_tape",
                "badge": _("Включено"),
                "hint": _("Укріплення задньої частини шиї, підвищений комфорт."),
                "auto_include_condition": "premium_or_oversize"
            }
        ],
        "pricing": {
            "base": 900,
            "premium_delta": 0,
            "oversize_delta": 0,
            "extra_zone_delta": 160,
            "add_on_delta": 0,
        },
    },
    "customer_garment": {
        "label": _("Свій одяг"),
        "eyebrow": _("Через менеджера"),
        "summary": _("Надішли фото або опис — менеджер порахує вручну."),
        "hero_note": _("Головне тут: опис виробу, зони і чітке формулювання задачі."),
        "fits": [],
        "fabrics": {},
        "default_fit": "",
        "default_fabric": "",
        "colors": [
            {"value": "own", "label": _("Ваш колір"), "hex": "#6d6d6d"},
        ],
        "default_color": "own",
        "zones": ["front", "back", "custom"],
        "default_zones": [],
                "add_ons": [
            {
                "value": "ribbed_neck",
                "label": _("Щільна горловина (Рібана)"),
                "price_delta": 0,
                "icon": "ribbed_neck",
                "badge": _("Включено"),
                "hint": _("Еластична горловина, що довго не втрачає форму."),
                "auto_include_condition": "premium_or_oversize"
            },
            {
                "value": "twill_tape",
                "label": _("Кіперна стрічка"),
                "price_delta": 0,
                "icon": "twill_tape",
                "badge": _("Включено"),
                "hint": _("Укріплення задньої частини шиї, підвищений комфорт."),
                "auto_include_condition": "premium_or_oversize"
            }
        ],
        "pricing": {
            "base": None,
            "premium_delta": 0,
            "oversize_delta": 0,
            "extra_zone_delta": 0,
            "add_on_delta": 0,
        },
    },
}


# ── Display label resolvers (admin/notifications/UI) ─────────────────
#
# Поточна модель CustomPrintLead зберігає сирі слаги ("black", "premium",
# "regular") у полях fit/fabric/color_choice. Ці резолвери повертають
# людиноорієнтовані лейбли з PRODUCT_MATRIX, з фолбеком на статичні
# словники FIT_LABELS / FABRIC_LABELS, щоб старі ліди не зламалися.

def resolve_color_label(product_type: str, color_value: str, fabric_value: str = "") -> dict:
    """Повертає {"label": "Чорний", "hex": "#151515"} для color_choice.

    Якщо це термо-варіант (футболка oversize + thermo) — шукає в
    fabrics[fit][thermo].colors. Інакше — у головному products.colors.
    Для невідомого color_value повертає сам слаг як label.
    """
    if not color_value:
        return {"label": "", "hex": ""}
    matrix = PRODUCT_MATRIX.get(product_type) or {}

    # 1) Перевіряємо thermo-кольори (тільки для футболки + thermo тканини).
    if product_type == "tshirt" and fabric_value == "thermo":
        fabrics_by_fit = matrix.get("fabrics") or {}
        for fabric_list in fabrics_by_fit.values():
            for fabric_def in fabric_list or []:
                if fabric_def.get("value") != "thermo":
                    continue
                for c in fabric_def.get("colors") or []:
                    if c.get("value") == color_value:
                        return {
                            "label": str(c.get("label") or color_value),
                            "hex": c.get("hex") or "",
                        }

    # 2) Fit-specific colors take precedence over the broad product palette.
    fit_colors = matrix.get("fit_colors") or {}
    candidates = [color for palette in fit_colors.values() for color in palette or []]
    candidates.extend(matrix.get("colors") or [])
    for c in candidates:
        if c.get("value") == color_value:
            return {
                "label": str(c.get("label") or color_value),
                "hex": c.get("hex") or "",
            }

    return {"label": color_value, "hex": ""}


def resolve_fabric_label(product_type: str, fit_value: str, fabric_value: str) -> str:
    """Повертає 'Преміум' для 'premium' з PRODUCT_MATRIX[type].fabrics[fit][...].label.

    Fallback: FABRIC_LABELS, потім сам слаг.
    """
    if not fabric_value:
        return ""
    matrix = PRODUCT_MATRIX.get(product_type) or {}
    fabrics_map = matrix.get("fabrics") or {}
    candidates = []
    if fit_value and fabrics_map.get(fit_value):
        candidates.extend(fabrics_map[fit_value])
    # На випадок коли fit невідомий — пробігаємо всі fabrics.
    for fabrics_list in fabrics_map.values():
        for fabric_def in fabrics_list or []:
            candidates.append(fabric_def)
    for fabric_def in candidates:
        if fabric_def.get("value") == fabric_value:
            label = str(fabric_def.get("label") or "").strip()
            if label:
                return label
    return FABRIC_LABELS.get(fabric_value, fabric_value)


def resolve_fit_label(product_type: str, fit_value: str) -> str:
    """Повертає 'Класичний' для 'regular' з PRODUCT_MATRIX[type].fits[i].label."""
    if not fit_value:
        return ""
    matrix = PRODUCT_MATRIX.get(product_type) or {}
    for fit_def in matrix.get("fits") or []:
        if fit_def.get("value") == fit_value:
            label = str(fit_def.get("label") or "").strip()
            if label:
                return label
    return FIT_LABELS.get(fit_value, fit_value)


def resolve_lead_display_labels(lead) -> dict:
    """High-level helper: повертає дікт display-полів для lead-обʼєкта.

    Використовується в адмін-панелі та notification-формуванні.
    """
    product_type = getattr(lead, "product_type", "") or ""
    fit = getattr(lead, "fit", "") or ""
    fabric = getattr(lead, "fabric", "") or ""
    color_choice = getattr(lead, "color_choice", "") or ""
    color_info = resolve_color_label(product_type, color_choice, fabric)
    return {
        "fit_label": resolve_fit_label(product_type, fit),
        "fabric_label": resolve_fabric_label(product_type, fit, fabric),
        "fabric_value": fabric,
        "color_label": color_info["label"],
        "color_hex": color_info["hex"],
        "color_value": color_choice,
    }


# Короткі іконки + опис для тканин — щоб у адміна одразу видно що преміум.
FABRIC_BADGES = {
    "premium": {"emoji": "💎", "tone": "premium", "note": "320 г/м², акуратна обробка"},
    "thermo": {"emoji": "🌡", "tone": "thermo", "note": "реагує на тепло тіла"},
    "standard": {"emoji": "📦", "tone": "standard", "note": "базова щільність"},
}


def resolve_fabric_badge(fabric_value: str) -> dict:
    """Повертає {"emoji", "tone", "note"} для тканини або порожній dict."""
    return dict(FABRIC_BADGES.get(fabric_value or "", {}))


STAGE_PROFILES = {
    "hoodie": {
        "default_fit": "regular",
        "regular": {
            "front": {
                "view_box": "0 0 420 520",
                "svg_markup": _svg_markup(
                    "<path class='cp-stage-svg__part cp-stage-svg__part--shade' d='M126 130 C95 140 69 163 54 194 C40 223 35 264 39 308 C41 330 54 346 74 348 C89 349 101 341 108 328 L136 170 C139 156 136 141 126 130 Z'/>",
                    "<path class='cp-stage-svg__part cp-stage-svg__part--shade' d='M294 130 C325 140 351 163 366 194 C380 223 385 264 381 308 C379 330 366 346 346 348 C331 349 319 341 312 328 L284 170 C281 156 284 141 294 130 Z'/>",
                    "<path class='cp-stage-svg__part cp-stage-svg__part--base' d='M138 132 C158 108 183 94 210 94 C237 94 262 108 282 132 L294 148 C306 160 312 176 311 193 L302 446 C301 468 284 486 262 486 H158 C136 486 119 468 118 446 L109 193 C108 176 114 160 126 148 Z'/>",
                    "<path class='cp-stage-svg__part cp-stage-svg__part--top' d='M154 138 C156 98 179 66 210 66 C241 66 264 98 266 138 C255 150 241 158 226 162 H194 C179 158 165 150 154 138 Z'/>",
                    "<path class='cp-stage-svg__detail cp-stage-svg__detail--line' d='M175 154 C185 147 197 144 210 144 C223 144 235 147 245 154'/>",
                    "<path class='cp-stage-svg__detail cp-stage-svg__detail--line' d='M145 312 C163 303 186 298 210 298 C234 298 257 303 275 312'/>",
                    "<path class='cp-stage-svg__detail cp-stage-svg__detail--line' d='M162 314 C155 344 152 375 152 408 H268 C268 375 265 344 258 314'/>",
                    "<path class='cp-stage-svg__detail cp-stage-svg__detail--line' d='M147 473 H273'/>",
                ),
                "anchors": {
                    "front": _stage_anchor(
                        50,
                        42.5,
                        presets={
                            "A6": calc_iso_box("A6", body_width_mm=600, svg_body_width=204, svg_collar_y=138, top_offset_mm=230, x_center=58, radius=18),
                            "A5": calc_iso_box("A5", body_width_mm=600, svg_body_width=204, svg_collar_y=138, top_offset_mm=280, radius=19),
                            "A4": calc_iso_box("A4", body_width_mm=600, svg_body_width=204, svg_collar_y=138, top_offset_mm=280, radius=20),
                        },
                    ),
                    "kangaroo": _stage_anchor(
                        50,
                        68.5,
                        default=_stage_box(50, 68.5, 27.0, 13.5, 0, 18, "pocket"),
                    ),
                    "sleeve_left": _stage_anchor(
                        27.5,
                        45.5,
                        modes={
                            "a6": _stage_box(28.5, 47.8, 10.4, 15.8, 21, 16, "sleeve_patch"),
                            "full_text": _stage_box(24.8, 55.8, 8.2, 35.5, 19, 18, "sleeve_text"),
                        },
                    ),
                    "sleeve_right": _stage_anchor(
                        72.5,
                        45.5,
                        modes={
                            "a6": _stage_box(71.5, 47.8, 10.4, 15.8, -21, 16, "sleeve_patch"),
                            "full_text": _stage_box(75.2, 55.8, 8.2, 35.5, -19, 18, "sleeve_text"),
                        },
                    ),
                },
            },
            "back": {
                "view_box": "0 0 420 520",
                "svg_markup": _svg_markup(
                    "<path class='cp-stage-svg__part cp-stage-svg__part--shade' d='M124 136 C95 149 72 172 59 202 C47 230 42 271 46 314 C49 336 61 351 80 354 C95 356 106 348 112 335 L132 178 C135 163 133 147 124 136 Z'/>",
                    "<path class='cp-stage-svg__part cp-stage-svg__part--shade' d='M296 136 C325 149 348 172 361 202 C373 230 378 271 374 314 C371 336 359 351 340 354 C325 356 314 348 308 335 L288 178 C285 163 287 147 296 136 Z'/>",
                    "<path class='cp-stage-svg__part cp-stage-svg__part--base' d='M134 140 C154 119 181 108 210 108 C239 108 266 119 286 140 L296 152 C307 164 312 180 311 196 L302 446 C301 468 284 486 262 486 H158 C136 486 119 468 118 446 L109 196 C108 180 113 164 124 152 Z'/>",
                    "<path class='cp-stage-svg__part cp-stage-svg__part--top' d='M152 139 C160 107 182 86 210 86 C238 86 260 107 268 139 L248 163 C237 171 224 175 210 175 C196 175 183 171 172 163 Z'/>",
                    "<path class='cp-stage-svg__detail cp-stage-svg__detail--line' d='M176 178 C186 184 198 187 210 187 C222 187 234 184 244 178'/>",
                    "<path class='cp-stage-svg__detail cp-stage-svg__detail--line' d='M143 472 H277'/>",
                    "<path class='cp-stage-svg__detail cp-stage-svg__detail--line' d='M141 228 C162 214 186 208 210 208 C234 208 258 214 279 228'/>",
                ),
                "anchors": {
                    "back": _stage_anchor(
                        50,
                        44.5,
                        presets={
                            "A4": calc_iso_box("A4", body_width_mm=600, svg_body_width=204, svg_collar_y=140, top_offset_mm=380, radius=22),
                            "A3": calc_iso_box("A3", body_width_mm=600, svg_body_width=204, svg_collar_y=140, top_offset_mm=380, radius=22),
                            "A2": calc_iso_box("A2", body_width_mm=600, svg_body_width=204, svg_collar_y=140, top_offset_mm=380, radius=24),
                        },
                    ),
                    "sleeve_left": _stage_anchor(
                        27.2,
                        46.2,
                        modes={
                            "a6": _stage_box(27.2, 49.6, 10.5, 16.2, -19, 16, "sleeve_patch"),
                            "full_text": _stage_box(24.5, 57.4, 8.2, 36.2, -17, 18, "sleeve_text"),
                        },
                    ),
                    "sleeve_right": _stage_anchor(
                        72.8,
                        46.2,
                        modes={
                            "a6": _stage_box(72.8, 49.6, 10.5, 16.2, 19, 16, "sleeve_patch"),
                            "full_text": _stage_box(75.5, 57.4, 8.2, 36.2, 17, 18, "sleeve_text"),
                        },
                    ),
                },
            },
        },
        "oversize": {
            "front": {
                "view_box": "0 0 420 520",
                "svg_markup": _svg_markup(
                    "<path class='cp-stage-svg__part cp-stage-svg__part--shade' d='M108 152 C66 163 32 191 14 229 C-1 262 -5 302 0 342 C3 366 18 382 42 386 C63 389 81 380 91 364 L124 196 C129 177 124 162 108 152 Z'/>",
                    "<path class='cp-stage-svg__part cp-stage-svg__part--shade' d='M312 152 C354 163 388 191 406 229 C421 262 425 302 420 342 C417 366 402 382 378 386 C357 389 339 380 329 364 L296 196 C291 177 296 162 312 152 Z'/>",
                    "<path class='cp-stage-svg__part cp-stage-svg__part--base' d='M118 152 C145 124 177 110 210 110 C243 110 275 124 302 152 L318 170 C334 187 341 208 339 230 L328 452 C327 475 307 492 284 492 H136 C113 492 93 475 92 452 L81 230 C79 208 86 187 102 170 Z'/>",
                    "<path class='cp-stage-svg__part cp-stage-svg__part--top' d='M145 154 C150 104 176 70 210 70 C244 70 270 104 275 154 C262 168 246 177 229 180 H191 C174 177 158 168 145 154 Z'/>",
                    "<path class='cp-stage-svg__detail cp-stage-svg__detail--line' d='M169 173 C182 164 196 160 210 160 C224 160 238 164 251 173'/>",
                    "<path class='cp-stage-svg__detail cp-stage-svg__detail--line' d='M138 320 C161 309 185 304 210 304 C235 304 259 309 282 320'/>",
                    "<path class='cp-stage-svg__detail cp-stage-svg__detail--line' d='M163 323 C154 355 151 386 151 418 H269 C269 386 266 355 257 323'/>",
                    "<path class='cp-stage-svg__detail cp-stage-svg__detail--line' d='M141 478 H279'/>",
                ),
                "anchors": {
                    "front": _stage_anchor(
                        50,
                        43.8,
                        presets={
                            "A6": calc_iso_box("A6", body_width_mm=650, svg_body_width=220, svg_collar_y=154, top_offset_mm=230, x_center=58, radius=18),
                            "A5": calc_iso_box("A5", body_width_mm=650, svg_body_width=220, svg_collar_y=154, top_offset_mm=280, radius=19),
                            "A4": calc_iso_box("A4", body_width_mm=650, svg_body_width=220, svg_collar_y=154, top_offset_mm=280, radius=21),
                        },
                    ),
                    "kangaroo": _stage_anchor(
                        50,
                        69.5,
                        default=_stage_box(50, 69.5, 29.0, 14.0, 0, 18, "pocket"),
                    ),
                    "sleeve_left": _stage_anchor(
                        24.5,
                        47.5,
                        modes={
                            "a6": _stage_box(25.2, 50.2, 10.8, 16.8, 23, 17, "sleeve_patch"),
                            "full_text": _stage_box(21.6, 58.2, 8.4, 38.4, 20, 18, "sleeve_text"),
                        },
                    ),
                    "sleeve_right": _stage_anchor(
                        75.5,
                        47.5,
                        modes={
                            "a6": _stage_box(74.8, 50.2, 10.8, 16.8, -23, 17, "sleeve_patch"),
                            "full_text": _stage_box(78.4, 58.2, 8.4, 38.4, -20, 18, "sleeve_text"),
                        },
                    ),
                },
            },
            "back": {
                "view_box": "0 0 420 520",
                "svg_markup": _svg_markup(
                    "<path class='cp-stage-svg__part cp-stage-svg__part--shade' d='M106 158 C68 172 38 198 22 234 C8 267 4 307 9 347 C12 370 27 386 50 390 C69 393 85 385 94 370 L121 202 C126 183 121 168 106 158 Z'/>",
                    "<path class='cp-stage-svg__part cp-stage-svg__part--shade' d='M314 158 C352 172 382 198 398 234 C412 267 416 307 411 347 C408 370 393 386 370 390 C351 393 335 385 326 370 L299 202 C294 183 299 168 314 158 Z'/>",
                    "<path class='cp-stage-svg__part cp-stage-svg__part--base' d='M116 158 C144 132 177 118 210 118 C243 118 276 132 304 158 L319 174 C334 190 341 210 339 231 L328 452 C327 475 307 492 284 492 H136 C113 492 93 475 92 452 L81 231 C79 210 86 190 101 174 Z'/>",
                    "<path class='cp-stage-svg__part cp-stage-svg__part--top' d='M144 160 C154 118 179 92 210 92 C241 92 266 118 276 160 L252 184 C239 193 225 197 210 197 C195 197 181 193 168 184 Z'/>",
                    "<path class='cp-stage-svg__detail cp-stage-svg__detail--line' d='M172 195 C184 203 197 206 210 206 C223 206 236 203 248 195'/>",
                    "<path class='cp-stage-svg__detail cp-stage-svg__detail--line' d='M143 476 H277'/>",
                    "<path class='cp-stage-svg__detail cp-stage-svg__detail--line' d='M134 236 C159 219 184 212 210 212 C236 212 261 219 286 236'/>",
                ),
                "anchors": {
                    "back": _stage_anchor(
                        50,
                        45.8,
                        presets={
                            "A4": calc_iso_box("A4", body_width_mm=650, svg_body_width=220, svg_collar_y=140, top_offset_mm=380, radius=20),
                            "A3": calc_iso_box("A3", body_width_mm=650, svg_body_width=220, svg_collar_y=140, top_offset_mm=380, radius=21),
                            "A2": calc_iso_box("A2", body_width_mm=650, svg_body_width=220, svg_collar_y=140, top_offset_mm=380, radius=22),
                        },
                    ),
                    "sleeve_left": _stage_anchor(
                        24.2,
                        48.4,
                        modes={
                            "a6": _stage_box(24.5, 52.0, 11.0, 17.1, -20, 17, "sleeve_patch"),
                            "full_text": _stage_box(21.4, 60.4, 8.4, 39.0, -18, 18, "sleeve_text"),
                        },
                    ),
                    "sleeve_right": _stage_anchor(
                        75.8,
                        48.4,
                        modes={
                            "a6": _stage_box(75.5, 52.0, 11.0, 17.1, 20, 17, "sleeve_patch"),
                            "full_text": _stage_box(78.6, 60.4, 8.4, 39.0, 18, 18, "sleeve_text"),
                        },
                    ),
                },
            },
        },
    },
    "tshirt": {
        "default_fit": "regular",
        "regular": {
            "front": {
                "view_box": "0 0 420 520",
                "svg_markup": _svg_markup(
                    "<path class='cp-stage-svg__part cp-stage-svg__part--shade' d='M134 142 C105 149 83 162 69 182 C55 201 50 221 53 246 C55 261 65 272 79 275 C92 278 102 271 109 258 L131 188 C134 173 136 156 134 142 Z'/>",
                    "<path class='cp-stage-svg__part cp-stage-svg__part--shade' d='M286 142 C315 149 337 162 351 182 C365 201 370 221 367 246 C365 261 355 272 341 275 C328 278 318 271 311 258 L289 188 C286 173 284 156 286 142 Z'/>",
                    "<path class='cp-stage-svg__part cp-stage-svg__part--base' d='M143 142 C160 122 184 112 210 112 C236 112 260 122 277 142 L286 153 C294 162 298 174 297 187 L291 452 C290 472 274 488 254 488 H166 C146 488 130 472 129 452 L123 187 C122 174 126 162 134 153 Z'/>",
                    "<path class='cp-stage-svg__part cp-stage-svg__part--top' d='M168 139 C176 128 191 121 210 121 C229 121 244 128 252 139 C244 151 229 157 210 157 C191 157 176 151 168 139 Z'/>",
                    "<path class='cp-stage-svg__detail cp-stage-svg__detail--line' d='M165 140 C176 149 192 154 210 154 C228 154 244 149 255 140'/>",
                    "<path class='cp-stage-svg__detail cp-stage-svg__detail--line' d='M139 475 H281'/>",
                ),
                "anchors": {
                    "front": _stage_anchor(
                        50,
                        40.8,
                        presets={
                            "A6": _stage_box(50, 41.2, 15.4, 10.4, 0, 18, "panel"),
                            "A5": _stage_box(50, 41.8, 21.2, 14.0, 0, 19, "panel"),
                            "A4": _stage_box(50, 42.8, 28.6, 18.6, 0, 20, "panel"),
                        },
                    ),
                    "sleeve_left": _stage_anchor(
                        27.5,
                        29.6,
                        modes={
                            "a6": _stage_box(27.2, 31.8, 9.8, 12.8, 13, 16, "sleeve_patch"),
                            "full_text": _stage_box(25.1, 39.4, 7.4, 25.8, 14, 16, "sleeve_text"),
                        },
                    ),
                    "sleeve_right": _stage_anchor(
                        72.5,
                        29.6,
                        modes={
                            "a6": _stage_box(72.8, 31.8, 9.8, 12.8, -13, 16, "sleeve_patch"),
                            "full_text": _stage_box(74.9, 39.4, 7.4, 25.8, -14, 16, "sleeve_text"),
                        },
                    ),
                },
            },
            "back": {
                "view_box": "0 0 420 520",
                "svg_markup": _svg_markup(
                    "<path class='cp-stage-svg__part cp-stage-svg__part--shade' d='M134 146 C106 154 85 168 72 188 C60 207 55 226 58 250 C60 265 70 276 83 279 C95 282 104 275 110 263 L129 190 C133 174 135 159 134 146 Z'/>",
                    "<path class='cp-stage-svg__part cp-stage-svg__part--shade' d='M286 146 C314 154 335 168 348 188 C360 207 365 226 362 250 C360 265 350 276 337 279 C325 282 316 275 310 263 L291 190 C287 174 285 159 286 146 Z'/>",
                    "<path class='cp-stage-svg__part cp-stage-svg__part--base' d='M143 146 C161 126 185 116 210 116 C235 116 259 126 277 146 L286 157 C293 166 297 178 296 191 L290 452 C289 472 273 488 253 488 H167 C147 488 131 472 130 452 L124 191 C123 178 127 166 134 157 Z'/>",
                    "<path class='cp-stage-svg__part cp-stage-svg__part--top' d='M170 147 C177 140 190 136 210 136 C230 136 243 140 250 147 C242 156 229 160 210 160 C191 160 178 156 170 147 Z'/>",
                    "<path class='cp-stage-svg__detail cp-stage-svg__detail--line' d='M170 150 C181 157 195 160 210 160 C225 160 239 157 250 150'/>",
                    "<path class='cp-stage-svg__detail cp-stage-svg__detail--line' d='M140 475 H280'/>",
                ),
                "anchors": {
                    "back": _stage_anchor(
                        50,
                        41.8,
                        presets={
                            "A4": _stage_box(50, 43.6, 23.6, 29.8, 0, 20, "panel"),
                            "A3": _stage_box(50, 45.8, 29.2, 36.2, 0, 20, "panel"),
                            "A2": _stage_box(50, 48.8, 35.2, 43.4, 0, 22, "panel"),
                        },
                    ),
                    "sleeve_left": _stage_anchor(
                        27.6,
                        30.3,
                        modes={
                            "a6": _stage_box(27.2, 33.2, 9.8, 13.0, -12, 16, "sleeve_patch"),
                            "full_text": _stage_box(25.4, 40.8, 7.3, 26.0, -13, 16, "sleeve_text"),
                        },
                    ),
                    "sleeve_right": _stage_anchor(
                        72.4,
                        30.3,
                        modes={
                            "a6": _stage_box(72.8, 33.2, 9.8, 13.0, 12, 16, "sleeve_patch"),
                            "full_text": _stage_box(74.6, 40.8, 7.3, 26.0, 13, 16, "sleeve_text"),
                        },
                    ),
                },
            },
        },
        "oversize": {
            "front": {
                "view_box": "0 0 420 520",
                "svg_markup": _svg_markup(
                    "<path class='cp-stage-svg__part cp-stage-svg__part--shade' d='M118 168 C79 177 48 197 29 226 C13 251 8 278 12 308 C15 328 29 342 48 346 C64 349 79 342 89 328 L122 230 C128 208 129 188 118 168 Z'/>",
                    "<path class='cp-stage-svg__part cp-stage-svg__part--shade' d='M302 168 C341 177 372 197 391 226 C407 251 412 278 408 308 C405 328 391 342 372 346 C356 349 341 342 331 328 L298 230 C292 208 291 188 302 168 Z'/>",
                    "<path class='cp-stage-svg__part cp-stage-svg__part--base' d='M120 166 C148 140 179 128 210 128 C241 128 272 140 300 166 L316 183 C329 197 335 213 333 230 L323 454 C322 476 302 492 280 492 H140 C118 492 98 476 97 454 L87 230 C85 213 91 197 104 183 Z'/>",
                    "<path class='cp-stage-svg__part cp-stage-svg__part--top' d='M160 160 C171 148 189 142 210 142 C231 142 249 148 260 160 C248 172 231 178 210 178 C189 178 172 172 160 160 Z'/>",
                    "<path class='cp-stage-svg__detail cp-stage-svg__detail--line' d='M158 162 C171 172 189 176 210 176 C231 176 249 172 262 162'/>",
                    "<path class='cp-stage-svg__detail cp-stage-svg__detail--line' d='M138 478 H282'/>",
                ),
                "anchors": {
                    "front": _stage_anchor(
                        50,
                        41.8,
                        presets={
                            "A6": _stage_box(50, 42.6, 16.0, 10.8, 0, 18, "panel"),
                            "A5": _stage_box(50, 43.4, 22.4, 14.8, 0, 19, "panel"),
                            "A4": _stage_box(50, 44.6, 30.4, 19.8, 0, 21, "panel"),
                        },
                    ),
                    "sleeve_left": _stage_anchor(
                        24.8,
                        34.4,
                        modes={
                            "a6": _stage_box(24.8, 37.2, 10.2, 13.4, 12, 16, "sleeve_patch"),
                            "full_text": _stage_box(23.2, 46.6, 7.8, 28.8, 13, 16, "sleeve_text"),
                        },
                    ),
                    "sleeve_right": _stage_anchor(
                        75.2,
                        34.4,
                        modes={
                            "a6": _stage_box(75.2, 37.2, 10.2, 13.4, -12, 16, "sleeve_patch"),
                            "full_text": _stage_box(76.8, 46.6, 7.8, 28.8, -13, 16, "sleeve_text"),
                        },
                    ),
                },
            },
            "back": {
                "view_box": "0 0 420 520",
                "svg_markup": _svg_markup(
                    "<path class='cp-stage-svg__part cp-stage-svg__part--shade' d='M118 172 C81 181 51 202 33 231 C18 255 13 282 17 312 C20 332 34 346 53 350 C69 353 83 346 93 332 L121 236 C127 214 128 194 118 172 Z'/>",
                    "<path class='cp-stage-svg__part cp-stage-svg__part--shade' d='M302 172 C339 181 369 202 387 231 C402 255 407 282 403 312 C400 332 386 346 367 350 C351 353 337 346 327 332 L299 236 C293 214 292 194 302 172 Z'/>",
                    "<path class='cp-stage-svg__part cp-stage-svg__part--base' d='M120 170 C148 145 179 134 210 134 C241 134 272 145 300 170 L315 187 C328 201 334 217 332 234 L322 454 C321 476 301 492 279 492 H141 C119 492 99 476 98 454 L88 234 C86 217 92 201 105 187 Z'/>",
                    "<path class='cp-stage-svg__part cp-stage-svg__part--top' d='M162 170 C173 161 190 156 210 156 C230 156 247 161 258 170 C247 180 230 185 210 185 C190 185 173 180 162 170 Z'/>",
                    "<path class='cp-stage-svg__detail cp-stage-svg__detail--line' d='M161 172 C173 180 190 184 210 184 C230 184 247 180 259 172'/>",
                    "<path class='cp-stage-svg__detail cp-stage-svg__detail--line' d='M140 478 H280'/>",
                ),
                "anchors": {
                    "back": _stage_anchor(
                        50,
                        42.8,
                        presets={
                            # На oversize худи спина шире: svg_body_width ~ 220
                            "A4": calc_iso_box("A4", body_width_mm=650, svg_body_width=220, svg_collar_y=140, top_offset_mm=380, radius=20),
                            "A3": calc_iso_box("A3", body_width_mm=650, svg_body_width=220, svg_collar_y=140, top_offset_mm=380, radius=21),
                            "A2": calc_iso_box("A2", body_width_mm=650, svg_body_width=220, svg_collar_y=140, top_offset_mm=380, radius=22),
                        },
                    ),
                    "sleeve_left": _stage_anchor(
                        24.8,
                        35.0,
                        modes={
                            "a6": _stage_box(24.8, 38.2, 10.2, 13.6, -11, 16, "sleeve_patch"),
                            "full_text": _stage_box(23.5, 47.4, 7.6, 29.2, -12, 16, "sleeve_text"),
                        },
                    ),
                    "sleeve_right": _stage_anchor(
                        75.2,
                        35.0,
                        modes={
                            "a6": _stage_box(75.2, 38.2, 10.2, 13.6, 11, 16, "sleeve_patch"),
                            "full_text": _stage_box(76.5, 47.4, 7.6, 29.2, 12, 16, "sleeve_text"),
                        },
                    ),
                },
            },
        },
    },
    "longsleeve": {
        "default_fit": "default",
        "default": {
            "front": {
                "view_box": "0 0 420 520",
                "svg_markup": _svg_markup(
                    "<path class='cp-stage-svg__part cp-stage-svg__part--shade' d='M128 146 C96 158 71 181 55 214 C39 247 34 288 38 332 C40 354 53 370 73 374 C88 377 100 369 107 355 L134 194 C138 176 136 160 128 146 Z'/>",
                    "<path class='cp-stage-svg__part cp-stage-svg__part--shade' d='M292 146 C324 158 349 181 365 214 C381 247 386 288 382 332 C380 354 367 370 347 374 C332 377 320 369 313 355 L286 194 C282 176 284 160 292 146 Z'/>",
                    "<path class='cp-stage-svg__part cp-stage-svg__part--base' d='M140 145 C159 123 184 112 210 112 C236 112 261 123 280 145 L289 158 C298 168 302 182 301 196 L294 456 C293 475 278 490 259 490 H161 C142 490 127 475 126 456 L119 196 C118 182 122 168 131 158 Z'/>",
                    "<path class='cp-stage-svg__part cp-stage-svg__part--top' d='M169 143 C177 132 192 126 210 126 C228 126 243 132 251 143 C243 153 228 158 210 158 C192 158 177 153 169 143 Z'/>",
                    "<path class='cp-stage-svg__detail cp-stage-svg__detail--line' d='M168 145 C179 153 194 157 210 157 C226 157 241 153 252 145'/>",
                    "<path class='cp-stage-svg__detail cp-stage-svg__detail--line' d='M141 476 H279'/>",
                ),
                "anchors": {
                    "front": _stage_anchor(
                        50,
                        41.8,
                        presets={
                            "A6": _stage_box(50, 42.2, 15.7, 10.6, 0, 18, "panel"),
                            "A5": _stage_box(50, 42.8, 21.6, 14.2, 0, 19, "panel"),
                            "A4": _stage_box(50, 43.9, 29.2, 18.8, 0, 20, "panel"),
                        },
                    ),
                    "sleeve_left": _stage_anchor(
                        26.2,
                        46.2,
                        modes={
                            "a6": _stage_box(26.8, 49.4, 10.1, 16.0, 18, 16, "sleeve_patch"),
                            "full_text": _stage_box(23.8, 58.2, 8.0, 36.8, 17, 18, "sleeve_text"),
                        },
                    ),
                    "sleeve_right": _stage_anchor(
                        73.8,
                        46.2,
                        modes={
                            "a6": _stage_box(73.2, 49.4, 10.1, 16.0, -18, 16, "sleeve_patch"),
                            "full_text": _stage_box(76.2, 58.2, 8.0, 36.8, -17, 18, "sleeve_text"),
                        },
                    ),
                },
            },
            "back": {
                "view_box": "0 0 420 520",
                "svg_markup": _svg_markup(
                    "<path class='cp-stage-svg__part cp-stage-svg__part--shade' d='M128 150 C97 163 72 187 57 220 C43 252 38 294 42 338 C45 359 57 375 76 379 C92 382 103 374 109 360 L131 198 C135 180 135 164 128 150 Z'/>",
                    "<path class='cp-stage-svg__part cp-stage-svg__part--shade' d='M292 150 C323 163 348 187 363 220 C377 252 382 294 378 338 C375 359 363 375 344 379 C328 382 317 374 311 360 L289 198 C285 180 285 164 292 150 Z'/>",
                    "<path class='cp-stage-svg__part cp-stage-svg__part--base' d='M140 150 C159 128 184 118 210 118 C236 118 261 128 280 150 L289 163 C297 173 301 186 300 199 L293 456 C292 475 277 490 258 490 H162 C143 490 128 475 127 456 L120 199 C119 186 123 173 131 163 Z'/>",
                    "<path class='cp-stage-svg__part cp-stage-svg__part--top' d='M171 151 C178 144 191 140 210 140 C229 140 242 144 249 151 C241 160 228 164 210 164 C192 164 179 160 171 151 Z'/>",
                    "<path class='cp-stage-svg__detail cp-stage-svg__detail--line' d='M171 153 C181 160 194 164 210 164 C226 164 239 160 249 153'/>",
                    "<path class='cp-stage-svg__detail cp-stage-svg__detail--line' d='M142 476 H278'/>",
                ),
                "anchors": {
                    "back": _stage_anchor(
                        50,
                        42.6,
                        presets={
                            "A4": _stage_box(50, 44.4, 24.4, 30.6, 0, 20, "panel"),
                            "A3": _stage_box(50, 46.6, 30.0, 37.8, 0, 21, "panel"),
                            "A2": _stage_box(50, 49.6, 36.2, 45.0, 0, 22, "panel"),
                        },
                    ),
                    "sleeve_left": _stage_anchor(
                        26.0,
                        47.0,
                        modes={
                            "a6": _stage_box(26.0, 50.6, 10.1, 16.4, -17, 16, "sleeve_patch"),
                            "full_text": _stage_box(23.5, 59.2, 8.0, 37.2, -16, 18, "sleeve_text"),
                        },
                    ),
                    "sleeve_right": _stage_anchor(
                        74.0,
                        47.0,
                        modes={
                            "a6": _stage_box(74.0, 50.6, 10.1, 16.4, 17, 16, "sleeve_patch"),
                            "full_text": _stage_box(76.5, 59.2, 8.0, 37.2, 16, 18, "sleeve_text"),
                        },
                    ),
                },
            },
        },
    },
    "customer_garment": {
        "default_fit": "default",
        "default": {
            "front": {
                "view_box": "0 0 420 520",
                "svg_markup": _svg_markup(
                    "<path class='cp-stage-svg__part cp-stage-svg__part--base' d='M145 136 C164 118 186 110 210 110 C234 110 256 118 275 136 L285 149 C295 160 300 174 300 189 V454 C300 474 284 490 264 490 H156 C136 490 120 474 120 454 V189 C120 174 125 160 135 149 Z'/>",
                    "<path class='cp-stage-svg__part cp-stage-svg__part--top' d='M171 136 C179 125 193 119 210 119 C227 119 241 125 249 136 C241 147 227 153 210 153 C193 153 179 147 171 136 Z'/>",
                    "<path class='cp-stage-svg__detail cp-stage-svg__detail--line' d='M170 138 C180 147 194 151 210 151 C226 151 240 147 250 138'/>",
                    "<path class='cp-stage-svg__detail cp-stage-svg__detail--line' d='M141 476 H279'/>",
                ),
                "anchors": {
                    "front": _stage_anchor(
                        50,
                        42.6,
                        presets={
                            "A6": _stage_box(50, 43.0, 15.4, 10.4, 0, 18, "panel"),
                            "A5": _stage_box(50, 43.6, 21.4, 14.0, 0, 19, "panel"),
                            "A4": _stage_box(50, 44.8, 28.8, 18.6, 0, 20, "panel"),
                        },
                    ),
                    "custom": _stage_anchor(
                        34.5,
                        62.0,
                        default=_stage_box(35.5, 63.0, 23.5, 15.5, -8, 18, "custom_panel"),
                    ),
                },
            },
            "back": {
                "view_box": "0 0 420 520",
                "svg_markup": _svg_markup(
                    "<path class='cp-stage-svg__part cp-stage-svg__part--base' d='M145 142 C164 124 186 116 210 116 C234 116 256 124 275 142 L285 155 C295 166 300 180 300 195 V454 C300 474 284 490 264 490 H156 C136 490 120 474 120 454 V195 C120 180 125 166 135 155 Z'/>",
                    "<path class='cp-stage-svg__part cp-stage-svg__part--top' d='M172 145 C179 138 193 134 210 134 C227 134 241 138 248 145 C240 154 227 158 210 158 C193 158 180 154 172 145 Z'/>",
                    "<path class='cp-stage-svg__detail cp-stage-svg__detail--line' d='M171 147 C181 154 194 158 210 158 C226 158 239 154 249 147'/>",
                    "<path class='cp-stage-svg__detail cp-stage-svg__detail--line' d='M141 476 H279'/>",
                ),
                "anchors": {
                    "back": _stage_anchor(
                        50,
                        43.4,
                        presets={
                            "A4": _stage_box(50, 45.2, 23.8, 30.2, 0, 20, "panel"),
                            "A3": _stage_box(50, 47.6, 29.4, 37.2, 0, 21, "panel"),
                            "A2": _stage_box(50, 50.8, 35.8, 44.4, 0, 22, "panel"),
                        },
                    ),
                    "custom": _stage_anchor(
                        65.5,
                        62.0,
                        default=_stage_box(64.5, 63.0, 23.5, 15.5, 8, 18, "custom_panel"),
                    ),
                },
            },
        },
    },
}


# ── Realistic stage art (custom_print_stage_art) ────────────────────
# Підміняє плоскі silhouette-SVG на детальні реалістичні і перераховує
# якорі принт-зон фізично точно (мм -> svg) від метрик артів.
from .custom_print_stage_art import build_stage_art as _build_stage_art

_BODY_TOP_OFFSETS_MM = {
    # відступ верхнього краю принта від видимої лінії коміра/капюшона
    "front": {"A6": 40, "A5": 65, "A4": 65},
    "back": {"A4": 45, "A3": 45, "A2": 40},
}


def _fmt_dims_cm(w_mm: float, h_mm: float) -> str:
    def _cm(v: float) -> str:
        s = f"{v / 10:.1f}".rstrip("0").rstrip(".")
        return s.replace(".", ",")

    return f"{_cm(w_mm)} × {_cm(h_mm)} см"


def _apply_stage_art() -> None:
    art = _build_stage_art()
    for product, fits in art.items():
        profile = STAGE_PROFILES.get(product)
        if not profile:
            continue
        for fit, views in fits.items():
            fit_node = profile.get(fit)
            if not isinstance(fit_node, dict):
                continue
            for view, data in views.items():
                node = fit_node.get(view)
                if not isinstance(node, dict):
                    continue
                metrics = data["metrics"]
                node["svg_markup"] = data["svg"]
                scale = metrics["body_width_svg"] / metrics["body_width_mm"]
                anchors = node.get("anchors") or {}

                body_key = "front" if view == "front" else "back"
                anchor = anchors.get(body_key)
                if anchor and anchor.get("presets"):
                    offsets = _BODY_TOP_OFFSETS_MM[body_key]
                    new_presets = {}
                    for fmt, old_box in anchor["presets"].items():
                        w_mm, h_mm = ISO_SIZES.get(fmt, (210, 297))
                        x_center = 58 if (view == "front" and fmt == "A6") else 50
                        box = calc_iso_box(
                            fmt,
                            body_width_mm=metrics["body_width_mm"],
                            svg_body_width=metrics["body_width_svg"],
                            svg_collar_y=metrics["collar_y_svg"],
                            top_offset_mm=offsets.get(fmt, 60),
                            x_center=x_center,
                            radius=old_box.get("radius", 20),
                        )
                        box["dims"] = _fmt_dims_cm(w_mm, h_mm)
                        new_presets[fmt] = box
                    anchor["presets"] = new_presets
                    mid_box = list(new_presets.values())[len(new_presets) // 2]
                    anchor["button"] = {"x": mid_box["x"], "y": mid_box["y"]}

                for side in ("sleeve_left", "sleeve_right"):
                    s_anchor = anchors.get(side)
                    s_metrics = metrics.get(side)
                    if not s_anchor or not s_metrics:
                        continue
                    a6_w_pct = round(105 * scale / 420.0 * 100, 1)
                    a6_h_pct = round(148 * scale / 520.0 * 100, 1)
                    x_pct = round(s_metrics["cx"] / 420.0 * 100, 1)
                    y_pct = round(s_metrics["cy"] / 520.0 * 100, 1)
                    patch = _stage_box(x_pct, y_pct, a6_w_pct, a6_h_pct, s_metrics["angle"], 14, "sleeve_patch")
                    patch["dims"] = _fmt_dims_cm(105, 148)
                    s_len = metrics.get("sleeve_len") or {}
                    y_top = s_len.get("y_top", s_metrics["cy"] - 70)
                    y_bot = s_len.get("y_bot", s_metrics["cy"] + 70)
                    text_cy_pct = round((y_top + y_bot) / 2 / 520.0 * 100, 1)
                    text_h_pct = round((y_bot - y_top) * 0.82 / 520.0 * 100, 1)
                    text = _stage_box(x_pct, text_cy_pct, 7.6, text_h_pct, round(s_metrics["angle"] * 0.8, 1), 16, "sleeve_text")
                    s_anchor["modes"] = {"a6": patch, "full_text": text}
                    s_anchor["button"] = {"x": x_pct, "y": y_pct}


_apply_stage_art()


def _coerce_int(value, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _coerce_price(value):
    if value in (None, ""):
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _allowed_values(items):
    return {item["value"] for item in items}


def _allowed_color_options(product_config: dict, fit: str = "", fabric: str = "") -> list[dict]:
    """Return the palette for the selected fit/material combination."""
    fabrics = (product_config.get("fabrics") or {}).get(fit or product_config.get("default_fit") or "", [])
    selected_fabric = next((item for item in fabrics if item.get("value") == fabric), None)
    if selected_fabric and selected_fabric.get("colors"):
        return list(selected_fabric["colors"])
    fit_palette = (product_config.get("fit_colors") or {}).get(fit or "")
    return list(fit_palette or product_config.get("colors") or [])


def build_custom_print_config(
    *,
    submit_url: str,
    safe_exit_url: str,
    add_to_cart_url: str = "",
    track_event_url: str = "",
) -> dict:
    return {
        "version": 2,
        "storage_key": CUSTOM_PRINT_DRAFT_STORAGE_KEY,
        "submit_url": submit_url,
        "safe_exit_url": safe_exit_url,
        "add_to_cart_url": add_to_cart_url,
        "track_event_url": track_event_url,
        "telegram_manager_url": TELEGRAM_MANAGER_URL,
        "quick_start_modes": deepcopy(QUICK_START_MODES),  # legacy
        "modes": deepcopy(CLIENT_MODES),
        "starter_styles": deepcopy(STARTER_STYLES),  # legacy
        "artwork_services": deepcopy(ARTWORK_SERVICES),
        "triage_statuses": deepcopy(TRIAGE_STATUSES),
        "size_modes": deepcopy(SIZE_MODES),
        "contact_channels": deepcopy(CONTACT_CHANNELS),
        "zone_labels": deepcopy(ZONE_LABELS),
        "product_labels": deepcopy(PRODUCT_LABELS),
        "products": deepcopy(PRODUCT_MATRIX),
        "gift_service": deepcopy(GIFT_SERVICE),
        "b2b_tier": deepcopy(B2B_TIER),
        "size_grid": list(SIZE_GRID),
        "progress_steps": deepcopy(PROGRESS_STEPS),
        "ui_strings": deepcopy(UI_STRINGS),
        "preview_assets": deepcopy(PREVIEW_ASSETS),
        "preview_calibration": deepcopy(PREVIEW_CALIBRATION),
        "format_dimensions": deepcopy(FORMAT_DIMENSIONS),
        "front_size_presets": deepcopy(FRONT_SIZE_PRESETS),
        "front_size_default": FRONT_SIZE_DEFAULT,
        "back_size_presets": deepcopy(BACK_SIZE_PRESETS),
        "back_size_default": BACK_SIZE_DEFAULT,
        "sleeve_mode_options": deepcopy(SLEEVE_MODE_OPTIONS),
        "sleeve_mode_default": SLEEVE_MODE_DEFAULT,
        "stage_meta": deepcopy(STAGE_META),
        "stage_profiles": deepcopy(STAGE_PROFILES),
        "defaults": normalize_custom_print_snapshot({}),
    }


def _expand_print_placements(snapshot: dict) -> list[dict]:
    zones = list((snapshot.get("print") or {}).get("zones") or [])
    zone_options = (snapshot.get("print") or {}).get("zone_options") or {}
    front_sizes = {item["value"] for item in FRONT_SIZE_PRESETS}
    back_sizes = {item["value"] for item in BACK_SIZE_PRESETS}
    sleeve_modes = {item["value"] for item in SLEEVE_MODE_OPTIONS}
    entries = []

    for index, zone in enumerate(zones):
        options = zone_options.get(zone) if isinstance(zone_options, dict) else {}
        if not isinstance(options, dict):
            options = {}

        if zone == "sleeve":
            left_enabled = bool(options.get("left_enabled"))
            right_enabled = bool(options.get("right_enabled"))
            if not left_enabled and not right_enabled:
                left_enabled = True
            for side in ("left", "right"):
                enabled = left_enabled if side == "left" else right_enabled
                if not enabled:
                    continue
                mode = str(options.get(f"{side}_mode") or SLEEVE_MODE_DEFAULT).strip()
                if mode not in sleeve_modes:
                    mode = SLEEVE_MODE_DEFAULT
                text = str(options.get(f"{side}_text") or "").strip()
                entry = {
                    "zone": "sleeve",
                    "placement_key": f"sleeve_{side}",
                    "label": ZONE_LABELS.get(f"sleeve_{side}", f"sleeve_{side}"),
                    "side": side,
                    "mode": mode,
                    "text": text,
                    "top_level_index": index,
                }
                scene_preview = options.get(f"{side}_scene_preview")
                if isinstance(scene_preview, dict) and scene_preview:
                    entry["scene_preview"] = deepcopy(scene_preview)
                entries.append(entry)
            continue

        entry = {
            "zone": zone,
            "placement_key": zone,
            "label": ZONE_LABELS.get(zone, zone),
            "top_level_index": index,
        }
        size_preset = str(options.get("size_preset") or "").upper()
        if zone == "front" and size_preset in front_sizes:
            entry["size_preset"] = size_preset
        elif zone == "back" and size_preset in back_sizes:
            entry["size_preset"] = size_preset
        scene_preview = options.get("scene_preview")
        if isinstance(scene_preview, dict) and scene_preview:
            entry["scene_preview"] = deepcopy(scene_preview)
        entries.append(entry)

    return entries


def build_placement_specs(snapshot: dict) -> list[dict]:
    specs = []
    artwork_file_index = 0
    for expanded_index, entry in enumerate(_expand_print_placements(snapshot)):
        zone = entry["zone"]
        requires_artwork_file = not (zone == "sleeve" and (entry.get("mode") or SLEEVE_MODE_DEFAULT) == "full_text")
        spec = {
            "zone": zone,
            "placement_key": entry.get("placement_key") or zone,
            "label": entry.get("label") or ZONE_LABELS.get(zone, zone),
            "variant": "standard" if expanded_index == 0 and zone in {"front", "back"} else "estimate",
            "is_free": expanded_index == 0,
            "format": "standard" if zone in {"front", "back"} else "custom",
            "size": "standard" if zone in {"front", "back"} else "manager_review",
            "attachment_role": "design",
            "requires_artwork_file": requires_artwork_file,
        }
        if requires_artwork_file:
            spec["file_index"] = artwork_file_index
            artwork_file_index += 1
        if "size_preset" in entry:
            spec["size_preset"] = entry["size_preset"]
            spec["size"] = entry["size_preset"]
        if zone == "sleeve":
            spec["side"] = entry.get("side")
            spec["mode"] = entry.get("mode") or SLEEVE_MODE_DEFAULT
            if spec["mode"] == "full_text":
                spec["format"] = "text_vertical"
                spec["size"] = "full_sleeve"
            else:
                spec["size"] = "A6"
            if entry.get("text"):
                spec["text"] = entry["text"]
        if "scene_preview" in entry:
            spec["scene_preview"] = deepcopy(entry["scene_preview"])
        specs.append(spec)
    return specs


def normalize_custom_print_snapshot(raw_snapshot: dict | None) -> dict:
    raw_snapshot = raw_snapshot or {}

    quick_start_mode = (raw_snapshot.get("quick_start_mode") or "start_blank").strip()
    if quick_start_mode not in _allowed_values(QUICK_START_MODES):
        quick_start_mode = "start_blank"

    mode = (raw_snapshot.get("mode") or "personal").strip()
    if mode not in {"personal", "brand"}:
        mode = "personal"

    product_payload = raw_snapshot.get("product") or {}
    product_type = (product_payload.get("type") or "hoodie").strip()
    if product_type not in PRODUCT_MATRIX:
        product_type = "hoodie"
    product_config = PRODUCT_MATRIX[product_type]

    fit = (product_payload.get("fit") or product_config.get("default_fit") or "").strip()
    fit_choices = {item["value"] for item in product_config.get("fits") or []}
    if fit_choices and fit not in fit_choices:
        fit = product_config.get("default_fit") or next(iter(fit_choices))
    if not fit_choices:
        fit = ""

    fabric_choices = {
        item["value"]
        for item in (product_config.get("fabrics") or {}).get(fit or product_config.get("default_fit") or "", [])
        if item.get("available", True) is not False
    }
    fabric = (product_payload.get("fabric") or product_config.get("default_fabric") or "").strip()
    if fabric_choices and fabric not in fabric_choices:
        available_fabrics = [
            item for item in (product_config.get("fabrics") or {}).get(fit or product_config.get("default_fit") or "", [])
            if item.get("available", True) is not False
        ]
        fabric = (
            next((item["value"] for item in available_fabrics if item.get("included_in_base")), None)
            or (available_fabrics[0]["value"] if available_fabrics else next(iter(fabric_choices)))
        )
    if not fabric_choices:
        fabric = product_config.get("default_fabric", "")

    color_options = _allowed_color_options(product_config, fit, fabric)
    color_choices = {item["value"] for item in color_options}
    color = (product_payload.get("color") or product_config.get("default_color") or "").strip()
    if color_choices and color not in color_choices:
        default_color = product_config.get("default_color")
        color = default_color if default_color in color_choices else next(iter(color_choices))

    print_payload = raw_snapshot.get("print") or {}
    available_zones = set(product_config.get("zones") or [])
    zones = []
    for zone in print_payload.get("zones") or []:
        if zone in available_zones and zone not in zones:
            zones.append(zone)

    zone_options = {}
    raw_zone_options = print_payload.get("zone_options") or {}
    allowed_front_sizes = {item["value"] for item in FRONT_SIZE_PRESETS}
    allowed_back_sizes = {item["value"] for item in BACK_SIZE_PRESETS}
    allowed_sleeve_modes = {item["value"] for item in SLEEVE_MODE_OPTIONS}
    if isinstance(raw_zone_options, dict):
        for zone, raw_options in raw_zone_options.items():
            if zone not in available_zones or zone not in zones or not isinstance(raw_options, dict):
                continue
            normalized_options = {}
            if zone == "front":
                size_preset = str(raw_options.get("size_preset") or "").upper()
                if size_preset not in allowed_front_sizes:
                    size_preset = FRONT_SIZE_DEFAULT
                normalized_options["size_preset"] = size_preset
            elif zone == "back":
                size_preset = str(raw_options.get("size_preset") or "").upper()
                if size_preset not in allowed_back_sizes:
                    size_preset = BACK_SIZE_DEFAULT
                normalized_options["size_preset"] = size_preset
            elif zone == "sleeve":
                left_enabled = bool(raw_options.get("left_enabled"))
                right_enabled = bool(raw_options.get("right_enabled"))
                if raw_options.get("mode") and "left_mode" not in raw_options:
                    left_enabled = True
                    raw_options = {
                        **raw_options,
                        "left_mode": raw_options.get("mode"),
                        "left_text": raw_options.get("text"),
                    }
                if not left_enabled and not right_enabled:
                    left_enabled = True
                normalized_options["left_enabled"] = left_enabled
                normalized_options["right_enabled"] = right_enabled
                for side in ("left", "right"):
                    mode = str(raw_options.get(f"{side}_mode") or SLEEVE_MODE_DEFAULT).strip()
                    if mode not in allowed_sleeve_modes:
                        mode = SLEEVE_MODE_DEFAULT
                    normalized_options[f"{side}_mode"] = mode
                    normalized_options[f"{side}_text"] = str(raw_options.get(f"{side}_text") or "").strip()[:120]
                    scene_preview = raw_options.get(f"{side}_scene_preview")
                    if isinstance(scene_preview, dict) and scene_preview:
                        normalized_options[f"{side}_scene_preview"] = deepcopy(scene_preview)
            scene_preview = raw_options.get("scene_preview")
            if zone in {"front", "back"} and isinstance(scene_preview, dict) and scene_preview:
                normalized_options["scene_preview"] = deepcopy(scene_preview)
            if normalized_options:
                zone_options[zone] = normalized_options
    if "front" in zones and "front" not in zone_options:
        zone_options["front"] = {"size_preset": FRONT_SIZE_DEFAULT}
    if "back" in zones and "back" not in zone_options:
        zone_options["back"] = {"size_preset": BACK_SIZE_DEFAULT}
    if "sleeve" in zones and "sleeve" not in zone_options:
        zone_options["sleeve"] = {
            "left_enabled": True,
            "right_enabled": False,
            "left_mode": SLEEVE_MODE_DEFAULT,
            "left_text": "",
            "right_mode": SLEEVE_MODE_DEFAULT,
            "right_text": "",
        }

    add_on_choices = {item["value"] for item in product_config.get("add_ons") or []}
    add_ons = []
    raw_add_ons = print_payload.get("add_ons") or []
    for add_on in raw_add_ons:
        # Legacy compat: old hoodie drafts with inside_label/hem_tag/grommets → collapse to lacing.
        if product_type == "hoodie" and add_on in {"inside_label", "hem_tag", "grommets"}:
            add_on = "lacing"
        if add_on in add_on_choices and add_on not in add_ons:
            add_ons.append(add_on)

    artwork_payload = raw_snapshot.get("artwork") or {}
    service_kind = (artwork_payload.get("service_kind") or "").strip()
    if service_kind not in SERVICE_LABELS:
        service_kind = ""

    files = []
    for index, item in enumerate(artwork_payload.get("files") or []):
        if not isinstance(item, dict):
            continue
        zone = item.get("zone")
        if zone not in available_zones:
            zone = zones[min(index, len(zones) - 1)] if zones else ""
        status = (item.get("status") or "").strip()
        if status not in TRIAGE_LABELS:
            status = "needs-review"
        files.append(
            {
                "name": str(item.get("name") or "").strip(),
                "zone": zone,
                "status": status,
                "role": str(item.get("role") or "design").strip() or "design",
            }
        )

    triage_status = (artwork_payload.get("triage_status") or "").strip()
    if triage_status not in TRIAGE_LABELS:
        if service_kind == "ready":
            triage_status = "print-ready" if files else "needs-review"
        elif service_kind == "adjust":
            triage_status = "needs-work"
        elif files:
            triage_status = "reference-only"
        else:
            triage_status = "needs-review"

    order_payload = raw_snapshot.get("order") or {}
    size_mode = (order_payload.get("size_mode") or "single").strip()
    if size_mode not in _allowed_values(SIZE_MODES):
        size_mode = "single"

    raw_size_breakdown = order_payload.get("size_breakdown") or {}
    if not isinstance(raw_size_breakdown, dict):
        raw_size_breakdown = {}
    size_breakdown = {}
    for key in SIZE_GRID:
        try:
            qty = int(raw_size_breakdown.get(key, 0) or 0)
        except (TypeError, ValueError):
            qty = 0
        if qty < 0:
            qty = 0
        size_breakdown[key] = qty

    gift_payload = order_payload.get("gift")
    if isinstance(gift_payload, dict):
        gift_enabled = bool(gift_payload.get("enabled"))
        gift_text = str(gift_payload.get("text") or "").strip()
    else:
        gift_enabled = bool(gift_payload)
        gift_text = str(order_payload.get("gift_text") or "").strip()

    contact_payload = raw_snapshot.get("contact") or {}
    channel = (contact_payload.get("channel") or "").strip()
    if channel not in _allowed_values(CONTACT_CHANNELS):
        channel = ""

    pricing_payload = raw_snapshot.get("pricing") or {}
    notes_payload = raw_snapshot.get("notes") or {}
    current_step = str(((raw_snapshot.get("ui") or {}).get("current_step") or "mode")).strip() or "mode"

    submission_type = (raw_snapshot.get("submission_type") or "lead").strip()
    if submission_type not in {"lead", "cart", "safe_exit"}:
        submission_type = "lead"

    return {
        "version": 2,
        "submission_type": submission_type,
        "quick_start_mode": quick_start_mode,
        "mode": mode,
        "starter_style": str(raw_snapshot.get("starter_style") or "").strip(),
        "product": {
            "type": product_type,
            "fit": fit,
            "fabric": fabric,
            "color": color,
        },
        "print": {
            "zones": zones,
            "add_ons": add_ons,
            "placement_note": str(print_payload.get("placement_note") or "").strip(),
            "zone_options": zone_options,
        },
        "artwork": {
            "service_kind": service_kind,
            "triage_status": triage_status,
            "files": files,
        },
        "order": {
            "quantity": _coerce_int(order_payload.get("quantity"), 1),
            "size_mode": size_mode,
            "sizes_note": str(order_payload.get("sizes_note") or "").strip(),
            "size_breakdown": size_breakdown,
            "gift": gift_enabled,
            "gift_text": gift_text,
        },
        "contact": {
            "channel": channel,
            "name": str(contact_payload.get("name") or "").strip(),
            "value": str(contact_payload.get("value") or "").strip(),
        },
        "pricing": {
            "base_price": _coerce_price(pricing_payload.get("base_price")),
            "design_price": _coerce_price(pricing_payload.get("design_price")),
            "addons_price": _coerce_price(pricing_payload.get("addons_price")),
            "print_price": _coerce_price(pricing_payload.get("print_price")),
            "zones_price": _coerce_price(pricing_payload.get("zones_price")),
            "gift_price": _coerce_price(pricing_payload.get("gift_price")),
            "discount_percent": _coerce_price(pricing_payload.get("discount_percent")),
            "discount_amount": _coerce_price(pricing_payload.get("discount_amount")),
            "b2b_discount_per_unit": _coerce_price(pricing_payload.get("b2b_discount_per_unit")),
            "unit_total": _coerce_price(pricing_payload.get("unit_total")),
            "final_total": _coerce_price(pricing_payload.get("final_total")),
            "estimate_required": bool(pricing_payload.get("estimate_required")),
            "estimate_reason": str(pricing_payload.get("estimate_reason") or "").strip(),
        },
        "notes": {
            "brand_name": str(notes_payload.get("brand_name") or "").strip(),
            "brief": str(notes_payload.get("brief") or "").strip(),
            "garment_note": str(notes_payload.get("garment_note") or "").strip(),
        },
        "ui": {
            "current_step": current_step,
        },
    }


def compute_cart_label(snapshot: dict) -> str:
    product_type = ((snapshot.get("product") or {}).get("type") or "hoodie").strip()
    label = PRODUCT_LABELS.get(product_type, product_type or "Кастом")
    zones = [ZONE_LABELS.get(z, z) for z in ((snapshot.get("print") or {}).get("zones") or [])]
    suffix = f" · {', '.join(zones)}" if zones else ""
    return f"Кастом · {label}{suffix}"
