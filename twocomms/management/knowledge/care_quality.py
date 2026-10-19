"""Owner statements Q05–07: attribution and care conditions are inseparable."""
VERSION = "2026-10-18.owner-care-quality.1"
SOURCE = "owner:2026-10-18:Q05,Q06,Q07"
REVIEW = "reviewed:owner-brief:2026-10-18"
CURRENT_BASES = ("current_tshirt_regular", "current_tshirt_oversize", "current_hoodie")
FACTS = (
    ("current_care", "current_tshirt_and_hoodie_bases", "care", CURRENT_BASES, {
        "uk": "Поточні футболки й худі радимо прати при 30°C, дотримуючись рекомендацій догляду, без агресивної хімії.",
        "ru": "Текущие футболки и худи рекомендуем стирать при 30°C, соблюдая рекомендации ухода, без агрессивной химии.",
        "en": "For current T-shirts and hoodies, wash at 30°C, follow the care guidance and avoid harsh chemicals.",
    }, "No zero-shrinkage, hot drying, 60°C durability or arbitrary-treatment promise. Follow the actual garment label for details not established here."),
    ("current_print_quality", "current_tshirt_and_hoodie_prints", "quality", CURRENT_BASES, {
        "uk": "За оцінкою засновника, нанесення витримує понад 100 прань за рекомендованого догляду при 30°C. Це заява власника; конкретну проблему команда розглядає окремо.",
        "ru": "По оценке основателя, нанесение выдерживает более 100 стирок при рекомендованном уходе при 30°C. Это заявление владельца; конкретную проблему команда рассматривает отдельно.",
        "en": "The founder estimates over 100 washes with the recommended care at 30°C. This is an owner statement; the team reviews individual issues separately.",
    }, "Always preserve owner attribution and care conditions with the number. No invented laboratory test/protocol, best-in-Ukraine ranking, eternal or never-cracks claim, automatic replacement after six months, or guessed print-head model. Existing close-up/video may be offered only with an approved asset; requesting new footage is not proof it already exists."),
)
