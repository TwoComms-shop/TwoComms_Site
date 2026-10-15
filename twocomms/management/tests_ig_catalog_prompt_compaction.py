"""Pure regressions: compact prompts preserve the complete catalogue source."""
import re
import unittest

from management.services.ig_catalog_prompt_compaction import (
    END, START, compact_catalog_text, expand_compact_catalog_text, fit_compact_catalog,
)


HEADER = 'Каталог TwoComms (усі актуальні товари; скорочений формат):'
SHIRT = ('ціни: fit=classic=1100 грн, fit=oversize=1250 грн; '
    'фасони/розміри: classic=S/M/L/XL/XXL; oversize=XS/S/M/L/XL/XXL')


def catalogue(count=40):
    lines = [HEADER]
    for number in range(count, 0, -1):
        lines.append(f'• id={number} | Футболка {number} — 1100-1250 грн; '
            f'кольори: Чорний (variant_id={number+100}, {SHIRT}); '
            f'принт: Унікальний принт {number}: кіт, написи «XS/M» і @звичайний.')
    return '\n'.join(lines)


class CatalogPromptCompactionTests(unittest.TestCase):
    def assert_round_trip(self, source, **options):
        encoded = compact_catalog_text(source, **options)
        restored = expand_compact_catalog_text(encoded)
        self.assertEqual(restored.encode('utf-8'), source.encode('utf-8'))
        return encoded

    def test_all_products_variants_colors_options_prices_sizes_and_prints_round_trip(self):
        source = catalogue()
        encoded = self.assert_round_trip(source)
        self.assertIn(START, encoded)
        self.assertLess(len(encoded), len(source)-3000)
        self.assertEqual(re.findall(r'^• id=\d+', encoded, re.M),
            re.findall(r'^• id=\d+', source, re.M))
        self.assertEqual(re.findall(r'variant_id=\d+', encoded),
            re.findall(r'variant_id=\d+', source))
        self.assertIn('id=1 | Футболка 1', encoded)
        self.assertIn('Унікальний принт 1:', encoded)
        self.assertIn('classic=S/M/L/XL/XXL', encoded)
        self.assertIn('fit=oversize=1250 грн', encoded)

    def test_thermo_exception_sizes_and_nested_reason_remain_variant_scoped(self):
        source = catalogue() + (
            '\n• id=999 | Термо — 1250 грн; кольори: Термо-зелена '
            '(variant_id=999, ціна 1250 грн, фасони/розміри: oversize=XS/M, '
            'причина: Термохромна тканина (спеціальна), на складі 5 шт)'
        )
        encoded = self.assert_round_trip(source)
        self.assertIn('Термо-зелена (variant_id=999, ціна 1250 грн', encoded)
        self.assertIn('oversize=XS/M', encoded)
        self.assertIn('Термохромна тканина (спеціальна)', encoded)
        self.assertIn('на складі 5 шт', encoded)
        # No generic common-size fallback is attached to the exceptional row.
        exceptional = next(row for row in encoded.splitlines() if row.startswith('• id=999'))
        self.assertNotIn('@C', exceptional)

    def test_varying_stock_is_local_and_does_not_prevent_common_contract(self):
        rows = [HEADER]
        for number in range(1, 35):
            rows.append(f'• id={number} | Річ {number} — 1100-1250 грн; кольори: '
                f'Білий (variant_id={number}, {SHIRT}, на складі {number} шт)')
        source = '\n'.join(rows)
        encoded = self.assert_round_trip(source)
        self.assertIn(START, encoded)
        for number in range(1, 35):
            self.assertRegex(encoded, rf'variant_id={number}, @C\d+, на складі {number} шт\)')

    def test_product_fallback_sizes_and_discount_price_are_exact_shared_values(self):
        source = '\n'.join([HEADER, *(
            f'• id={n} | Лонгслів «Код — Це Математика» {n} — '
            '1518 грн (знижка 8% врахована); кольори: Чорний '
            f'(variant_id={n}, ціна 1518 грн, фасони: classic); '
            'фасони/розміри: default: S/M/L/XL/XXL; принт: рисунок'
            for n in range(1, 40))])
        encoded = self.assert_round_trip(source)
        self.assertIn('1518 грн (знижка 8% врахована)', encoded)
        self.assertIn('; фасони/розміри: default: S/M/L/XL/XXL', encoded)
        self.assertIn('«Код — Це Математика» 1 — @C', encoded)

    def test_legend_overhead_prevents_unprofitable_encoding(self):
        source = HEADER + '\n• id=1 | Річ — 100 грн; кольори: Білий (variant_id=1, ціна 100 грн)'
        self.assertEqual(compact_catalog_text(source), source)
        self.assertEqual(compact_catalog_text(catalogue(2)), catalogue(2))

    def test_reserved_source_reference_is_not_interpreted_as_a_new_dictionary(self):
        for literal in ('@C1', '(v=99, підпис)', START):
            source = catalogue() + '; принт: буквальний ' + literal
            self.assertEqual(compact_catalog_text(source), source)

    def test_encoding_is_deterministic_idempotent_and_preserves_newlines(self):
        for source in (catalogue(), catalogue()+'\n', catalogue().replace('\n', '\r\n')+'\r\n'):
            encoded = self.assert_round_trip(source)
            self.assertEqual(compact_catalog_text(source), encoded)
            self.assertEqual(compact_catalog_text(encoded), encoded)

    def test_optional_variant_alias_is_explicit_and_exactly_reversible(self):
        encoded = self.assert_round_trip(catalogue(), abbreviate_variant_ids=True)
        self.assertIn('(v=N означає (variant_id=N', encoded)
        self.assertIn('(v=101, ', encoded)
        self.assertNotIn('(variant_id=101, ', encoded)

    def test_all_legend_rules_and_products_are_counted_in_budget(self):
        lines = catalogue().splitlines()
        footer = '\nПравило ціни: лише точна обрана конфігурація.'
        complete = compact_catalog_text('\n'.join(lines)+footer)
        text, dropped = fit_compact_catalog(lines, limit=len(complete), footer=footer)
        self.assertEqual((text, dropped), (complete, 0))
        self.assertIn('id=1 | Футболка 1', text)
        text, dropped = fit_compact_catalog(lines, limit=len(complete)-300, footer=footer)
        self.assertGreater(dropped, 0)
        self.assertLessEqual(len(text), len(complete)-300)
        self.assertIn(f'{dropped} не вміщено', text)
        self.assertIn('не означає відсутність товару чи залишку', text)
        self.assertTrue(text.endswith(footer))
        expanded = expand_compact_catalog_text(text)
        self.assertNotRegex(expanded, r'@C\d+')
        kept = len(re.findall(r'^• id=', expanded, re.M))
        self.assertEqual(kept+dropped, len(lines)-1)

    def test_impossible_budget_never_cuts_a_price_or_a_mandatory_rule(self):
        with self.assertRaisesRegex(ValueError, 'coverage_and_rules'):
            fit_compact_catalog(catalogue().splitlines(), limit=30, footer='\nПравило ціни: '+('x'*100))

    def test_unknown_or_corrupt_encoded_reference_is_rejected(self):
        encoded = compact_catalog_text(catalogue())
        with self.assertRaisesRegex(ValueError, 'reference_missing'):
            expand_compact_catalog_text(encoded+'\n@C999999')
        with self.assertRaisesRegex(ValueError, 'legend_invalid'):
            expand_compact_catalog_text(encoded.replace(END, ''))


if __name__ == '__main__':
    unittest.main()
