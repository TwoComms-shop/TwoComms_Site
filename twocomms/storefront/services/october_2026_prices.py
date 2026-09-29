"""Reviewed September 30 production snapshot; fixed October 2026 targets.

This manifest is intentionally literal: reruns never add an increment again.
Only published tshirts/hoodies in this snapshot may be changed.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class PriceChange:
    product_id: int
    slug: str
    category: str
    old_price: int
    old_discount: int | None
    new_price: int
    variants: tuple[tuple[int, int | None], ...]


MANIFEST_VERSION = "october-2026-v1"
COLLECTION_SLUG = "225"
THERMO_OLD_REASON = "термохромна тканина"

# product ID, exact slug/category, old price/discount, target, variant overrides.
PRICE_CHANGES = (
    PriceChange(1, 'classic-tshirt', 'tshirts', 950, 17, 1100, ((29, None),)),
    PriceChange(2, 'hoodie-classic', 'hoodie', 1950, 10, 1995, ((30, None),)),
    PriceChange(4, 'my-little-baby', 'tshirts', 950, 17, 1100, ((2, None), (3, None))),
    PriceChange(5, 'my-little-baby-hd', 'hoodie', 1850, 10, 1995, ((32, None),)),
    PriceChange(7, 'where-mi-present-ts', 'tshirts', 950, 17, 1100, ((4, None), (5, None))),
    PriceChange(8, 'where-mi-present-hd', 'hoodie', 1850, 10, 1995, ((34, None),)),
    PriceChange(10, 'in-shee', 'tshirts', 950, 17, 1100, ((36, None),)),
    PriceChange(11, 'in-shee-hd', 'hoodie', 1850, 10, 1850, ((37, None),)),
    PriceChange(13, 'business-money', 'tshirts', 950, 17, 1100, ((39, None),)),
    PriceChange(14, 'business-money-hd', 'hoodie', 1950, 10, 1995, ((40, None),)),
    PriceChange(16, 'last-breath', 'tshirts', 950, 17, 1100, ((42, None),)),
    PriceChange(17, 'last-breath-hd', 'hoodie', 1850, 10, 1995, ((43, None),)),
    PriceChange(19, 'kharkiv-district-ts', 'tshirts', 950, 17, 1100, ((6, None), (7, None))),
    PriceChange(20, 'kharkiv-district-hd', 'hoodie', 2080, 10, 1995, ((45, None),)),
    PriceChange(22, 'pokrovsk-girl', 'tshirts', 950, 17, 1100, ((47, None),)),
    PriceChange(23, 'pokrovsk-girl-hd', 'hoodie', 1850, 17, 1995, ((48, None),)),
    PriceChange(25, 'death-grabs-ass', 'tshirts', 950, 17, 1100, ((50, None),)),
    PriceChange(26, 'death-grabs-ass-hd', 'hoodie', 1850, 10, 1995, ((51, None),)),
    PriceChange(28, 'dvoznachni-summy', 'tshirts', 950, 17, 1100, ((53, None),)),
    PriceChange(29, 'dvoznachni-summy-hd', 'hoodie', 2000, 10, 1995, ((54, None),)),
    PriceChange(31, 'lord-of-the-lending', 'tshirts', 950, 17, 1100, ((8, None), (9, None))),
    PriceChange(32, 'lord-of-the-lending-hd', 'hoodie', 1850, 10, 1995, ((56, None),)),
    PriceChange(34, 'red-leaves-ts', 'tshirts', 950, 17, 1100, ((58, None),)),
    PriceChange(35, 'red-leaves-hd', 'hoodie', 1850, 10, 1995, ((59, None),)),
    PriceChange(37, 'death-gbs-ass-ts', 'tshirts', 950, 17, 1100, ((10, None), (11, None))),
    PriceChange(38, 'death-gbs-ass-hd', 'hoodie', 2155, 10, 1995, ((61, None),)),
    PriceChange(40, 'kha-edition-ts', 'tshirts', 1050, 15, 1100, ((63, None),)),
    PriceChange(41, 'kha-edition-hd', 'hoodie', 1850, 10, 1995, ((64, None),)),
    PriceChange(43, 'kha-style-ts', 'tshirts', 950, 17, 1100, ((66, None),)),
    PriceChange(44, 'kha-style-hd', 'hoodie', 1850, 10, 1995, ((67, None),)),
    PriceChange(46, 'pojuy-ts', 'tshirts', 950, 17, 1100, ((12, None), (13, None))),
    PriceChange(47, 'pojuy-hd', 'hoodie', 1950, 10, 1995, ((69, None),)),
    PriceChange(49, 'bentejne-ts', 'tshirts', 950, 17, 1100, ((14, None), (15, None))),
    PriceChange(50, 'bentejne-hd', 'hoodie', 1900, 10, 1995, ((71, None),)),
    PriceChange(91, '225-tshirt', 'tshirts', 660, None, 880, ((17, 800),)),
    PriceChange(92, '225-hoodie', 'hoodie', 1650, None, 1995, ((73, None),)),
    PriceChange(93, 'v2-0-pokrovsk', 'hoodie', 1950, 10, 1995, ((74, None),)),
    PriceChange(94, '20-twocomms-legend', 'hoodie', 1950, 7, 1995, ((75, None),)),
    PriceChange(95, 'hoodie-silent-winter', 'hoodie', 1950, 7, 1995, ((76, None),)),
    PriceChange(96, 'glory-of-ukraine-hd', 'hoodie', 1950, 7, 1995, ((77, None),)),
    PriceChange(98, 'ts-not-money', 'tshirts', 910, 10, 1100, ((19, None),)),
    PriceChange(100, 'hool-ts', 'tshirts', 950, 17, 1100, ((21, None),)),
    PriceChange(101, 'idea-hd', 'hoodie', 2550, 30, 1995, ((23, None),)),
    PriceChange(102, 'hd-twocomms-reality-bends-future-2026', 'hoodie', 2550, 25, 1995, ((24, None),)),
    PriceChange(103, 'twocomms-reality-bends-future-2026', 'tshirts', 1100, 20, 1100, ((25, None),)),
    PriceChange(104, 'twocomms-reality-bends-dark-neon-edition', 'tshirts', 1100, 12, 1100, ((26, None),)),
    PriceChange(105, 'ts-twocomms-reality-bends-mentol', 'tshirts', 1100, 20, 1100, ((27, None),)),
    PriceChange(106, 'twocomms-beliveidea-ts', 'tshirts', 1100, 20, 1100, ((28, None),)),
    PriceChange(107, 'futbolka-posmikhnys', 'tshirts', 1090, None, 1100, ((78, None),)),
    PriceChange(108, 'futbolka-bez-zhodnykh-sumniviv', 'tshirts', 1090, None, 1100, ((79, None),)),
    PriceChange(109, 'futbolka-kharkiv-forever', 'tshirts', 1090, None, 1100, ((80, None),)),
    PriceChange(110, 'futbolka-boiova-kvitochka', 'tshirts', 1090, None, 1100, ((81, 1050),)),
    PriceChange(111, 'futbolka-kharkiv-vokzalna', 'tshirts', 1090, None, 1100, ((82, None),)),
    PriceChange(112, 'futbolka-pravyl-nemaie', 'tshirts', 1090, None, 1100, ((83, None),)),
)

# Exact reviewed old option pricing; target fit pricing is declared below.
OPTION_PRICES = (
    (65, 91, 'fit=classic', 0, True),
    (66, 91, 'fit=oversize', 150, True),
    (53, 98, 'fit=classic', 0, True),
    (54, 98, 'fit=oversize', 0, True),
    (63, 107, 'fit=classic', 0, True),
    (64, 107, 'fit=oversize', 0, True),
    (61, 109, 'fit=classic', 0, True),
    (62, 109, 'fit=oversize', 0, True),
    (57, 110, 'fit=classic', 0, True),
    (58, 110, 'fit=oversize', 0, True),
    (59, 111, 'fit=classic', 0, True),
    (60, 111, 'fit=oversize', 0, True),
    (67, 112, 'fit=classic', 0, True),
    (56, 112, 'fit=oversize', 0, True),
)

# Offered fits are a guard, never a request to create/enable garment fits.
FIT_SNAPSHOT = (
    (1, 1, 'classic', True),
    (2, 1, 'oversize', True),
    (3, 4, 'classic', True),
    (4, 4, 'oversize', True),
    (5, 7, 'classic', True),
    (6, 7, 'oversize', True),
    (7, 10, 'classic', True),
    (8, 10, 'oversize', True),
    (9, 13, 'classic', True),
    (10, 13, 'oversize', True),
    (11, 16, 'classic', True),
    (12, 16, 'oversize', True),
    (13, 19, 'classic', True),
    (14, 19, 'oversize', True),
    (15, 22, 'classic', True),
    (16, 22, 'oversize', True),
    (17, 25, 'classic', True),
    (18, 25, 'oversize', True),
    (19, 28, 'classic', True),
    (20, 28, 'oversize', True),
    (21, 31, 'classic', True),
    (22, 31, 'oversize', True),
    (23, 34, 'classic', True),
    (24, 34, 'oversize', True),
    (25, 37, 'classic', True),
    (26, 37, 'oversize', True),
    (27, 40, 'classic', True),
    (28, 40, 'oversize', True),
    (29, 43, 'classic', True),
    (30, 43, 'oversize', True),
    (31, 46, 'classic', True),
    (32, 46, 'oversize', True),
    (33, 49, 'classic', True),
    (34, 49, 'oversize', True),
    (35, 91, 'classic', True),
    (36, 91, 'oversize', True),
    (37, 98, 'classic', True),
    (38, 98, 'oversize', True),
    (39, 100, 'classic', True),
    (40, 100, 'oversize', True),
    (41, 103, 'classic', True),
    (42, 103, 'oversize', True),
    (43, 104, 'classic', True),
    (44, 104, 'oversize', True),
    (45, 105, 'classic', True),
    (46, 105, 'oversize', True),
    (47, 106, 'classic', True),
    (48, 106, 'oversize', True),
    (57, 107, 'classic', True),
    (58, 107, 'oversize', True),
    (59, 108, 'classic', True),
    (60, 108, 'oversize', True),
    (55, 109, 'classic', True),
    (56, 109, 'oversize', True),
    (49, 110, 'classic', False),
    (50, 110, 'oversize', True),
    (53, 111, 'classic', True),
    (54, 111, 'oversize', True),
    (51, 112, 'classic', True),
    (52, 112, 'oversize', True),
)

# Applied only to active existing classic/oversize fits.
FIT_PRICE_DELTAS = {"classic": 0, "oversize": 150}
