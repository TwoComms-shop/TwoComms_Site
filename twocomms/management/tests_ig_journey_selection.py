from django.test import SimpleTestCase
from management.services.ig_journey_selection import selection_fields


class SelectionFieldsTests(SimpleTestCase):
    def state(self):
        return {'has_product': True, 'applicability_known': True,
                'product': {'title': 'Принт', 'kind': 'Худі'},
                'fit': {'required': True, 'selected': 'oversize', 'options': [{'code': 'oversize', 'label': 'Оверсайз'}]},
                'color': {'required': False, 'options': [{'name': 'Чорний'}]},
                'size': {'required': True, 'selected': ''}, 'quantity': 1}

    def project(self, state):
        return selection_fields(state, scope={'line_id': 'a'}, evidence_refs=[{'kind': 'message', 'id': 1}])

    def test_one_segment_per_required_field_with_catalog_type_and_fixed_color(self):
        result = self.project(self.state())
        self.assertEqual((result['completed'], result['total']), (4, 5))
        fields = {r['key']: r for r in result['items']}
        self.assertEqual(fields['kind']['value'], 'Худі')
        self.assertEqual(fields['fit']['value'], 'Оверсайз')
        self.assertEqual(fields['color']['value'], 'Чорний')
        self.assertFalse(fields['quantity']['required'])
        self.assertEqual(sum(r['key'] == 'size' for r in result['items']), 1)

    def test_unavailable_size_is_visible_but_not_completed(self):
        state = self.state()
        state['size']['requested_unavailable'] = 'M'
        result = self.project(state)
        row = next(r for r in result['items'] if r['key'] == 'size')
        self.assertEqual(row['value'], 'M')
        self.assertEqual(row['status'], 'invalidated')
        self.assertEqual(result['completed'], 4)

    def test_each_option_is_counted_once_disabled_choice_not_accepted(self):
        state = self.state()
        state['options'] = {'axes': [{'code': 'material', 'label': 'Матеріал', 'selected': 'cotton', 'choices': [{'code': 'cotton', 'label': 'Бавовна', 'is_enabled': False}]}]}
        result = self.project(state)
        self.assertEqual((result['completed'], result['total']), (4, 6))
        self.assertEqual(next(r for r in result['items'] if r['key'] == 'option:material')['status'], 'open')

    def test_unknown_applicability_has_no_fake_denominator_and_requires_sources(self):
        state = self.state()
        state['applicability_known'] = False
        self.assertIsNone(self.project(state)['total'])
        self.assertIsNone(selection_fields(state, scope={}, evidence_refs=[]))
