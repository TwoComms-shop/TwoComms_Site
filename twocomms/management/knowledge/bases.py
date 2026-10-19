"""Current named bases, reviewed against the owner's 18 October answers Q01–02.

Edits are proposals until reviewed, tested and deployed with the canonical
approved_public_facts owner. Product identity comes from the captured catalogue.
"""

VERSION = "2026-10-18.owner-bases.1"
SOURCE = "owner:2026-10-18:Q01,Q02"
REVIEW = "reviewed:owner-brief:2026-10-18"

# Separate provenance, deliberately not a claim that hoodies use kulir or
# that fabric origin establishes the garment's sewing country.
HOODIE_ORIGIN_EDITORIAL = {"source": "owner:earlier-input:retained-by-2026-10-18:Q01",
    "claim": "Turkish hoodie fabric", "scope": "hoodie_fabric_origin_only",
    "review_status": "exact_source_binding_pending"}

BASES = {
    "current_tshirt_regular": {"garment": "tshirt", "fit": "regular", "composition": ((95, "cotton"), (5, "elastane")), "gsm": 190, "fabric": "kulir_penye", "fabric_origin": "Turkey"},
    "current_tshirt_oversize": {"garment": "tshirt", "fit": "oversize", "composition": ((95, "cotton"), (5, "elastane")), "gsm": 220, "fabric": "kulir_penye", "fabric_origin": "Turkey"},
    "current_hoodie": {"garment": "hoodie", "fit": "slightly_oversize", "composition": ((90, "cotton"), (10, "polyester")), "gsm": 320, "fabric": "three_thread_penye", "fleece": True},
}

# key, scope, topic, base keys, public translations, provider boundary
FACTS = (
    ("current_tshirt_bases", "current_regular_and_oversize_tshirt_bases", "material", ("current_tshirt_regular", "current_tshirt_oversize"), {
        "uk": "Поточні футболки: 95% бавовни / 5% еластану, турецька кулірка пенье; regular — близько 190 г/м², oversize — близько 220 г/м².",
        "ru": "Текущие футболки: 95% хлопка / 5% эластана, турецкая кулирка пенье; regular — около 190 г/м², oversize — около 220 г/м².",
        "en": "Current T-shirts: 95% cotton / 5% elastane, Turkish combed kulir jersey; regular is approximately 190 g/m² and oversize approximately 220 g/m².",
    }, "Only the captured current matching T-shirt base. Approximate density, not a laboratory measurement of every garment. Fabric origin does not prove where the garment was sewn. No future-batch, stock or custom-material authority."),
    ("current_hoodie_bases", "current_hoodie_base", "material", ("current_hoodie",), {
        "uk": "Поточні худі: 90% бавовни / 10% поліестеру, тринитка пенье з флісом, близько 320 г/м².",
        "ru": "Текущие худи: 90% хлопка / 10% полиэстера, трёхнитка пенье с флисом, около 320 г/м².",
        "en": "Current hoodies: 90% cotton / 10% polyester, combed three-thread fleece, approximately 320 g/m².",
    }, "Only the captured current hoodie base. Three-thread fleece is not T-shirt kulir. Do not infer garment manufacturing country, later batches, or a specification for zip hoodies/longsleeves. Density is approximate."),
    ("current_hoodie_audience", "current_hoodie_base", "fit", ("current_hoodie",), {
        "uk": "Поточні худі — унісекс, одна злегка oversize основа. Бажану свободу й довжину обирають за мерками та особистим уподобанням.",
        "ru": "Текущие худи — унисекс, одна слегка oversize основа. Желаемую свободу и длину выбирают по меркам и личным предпочтениям.",
        "en": "Current hoodies are unisex and use one slightly oversize base. Choose ease and length using measurements and personal preference.",
    }, "Do not offer regular versus oversize as two physical hoodie cuts. Existing regular UI/preview is not proof of another cut or proof it has already been disabled. Do not guarantee fit or invent the pending hoodie chart."),
    ("unconfirmed_bases", "unconfirmed_or_future_base", "boundary", (), {
        "uk": "Для лонгслівів, інших непідтверджених основ і майбутніх партій потрібна окрема специфікація. Історія принта потребує точного design/version; схожий малюнок не є доказом.",
        "ru": "Для лонгсливов, других неподтверждённых основ и будущих партий нужна отдельная спецификация. Истории принтов требуют точного design/version; похожий рисунок не служит доказательством.",
        "en": "Longsleeves, other unconfirmed bases and future batches need their own specification. Print stories require exact design/version identity; a similar picture is not proof.",
    }, "Missing applicability is unknown. Ask the administration only for genuinely missing facts; an approved current composition needs no manager. No invented composition, density, origin, lab test, superiority or lifetime guarantee."),
)
