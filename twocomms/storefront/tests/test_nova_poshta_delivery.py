from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from django.urls import reverse

from orders.forms import TelegramNovaPoshtaWaybillForm
from orders.nova_poshta_documents import (
    TELEGRAM_CREATE_NP_WAYBILL_ACTION,
    TELEGRAM_DELETE_NP_WAYBILL_ACTION,
    NovaPoshtaDocumentError,
    NovaPoshtaDocumentService,
    NovaPoshtaResolvedPoint,
    build_waybill_description,
    normalize_phone,
)
from orders.telegram_notifications import TelegramNotifier
from orders.telegram_status_links import (
    build_order_action_token,
    build_order_action_url,
    verify_order_action_token,
)
from orders.nova_poshta_lookup import (
    NovaPoshtaDirectoryService,
    NovaPoshtaLookupUnavailable,
)
from orders.nova_poshta_checkout import (
    NovaPoshtaSelectionError,
    build_city_choice_token,
    build_warehouse_choice_token,
    resolve_delivery_selection,
)


class NovaPoshtaDirectoryServiceTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    @override_settings(
        NOVA_POSHTA_API_KEY='test-key',
        NOVA_POSHTA_API_URL='https://api.novaposhta.ua/v2.0/json/',
    )
    @patch('orders.nova_poshta_lookup.requests.post')
    def test_search_settlements_parses_search_response(self, mock_post):
        mock_response = Mock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            'success': True,
            'data': [
                {
                    'TotalCount': 1,
                    'Addresses': [
                        {
                            'Present': 'м. Київ, Київ',
                            'MainDescription': 'Київ',
                            'Area': 'Київська',
                            'Region': 'Київ',
                            'SettlementTypeDescription': 'місто',
                            'SettlementRef': 'settlement-ref',
                            'DeliveryCity': 'delivery-city-ref',
                            'Ref': 'legacy-city-ref',
                            'Warehouses': '1473',
                        }
                    ],
                }
            ],
            'errors': [],
            'warnings': [],
        }
        mock_post.return_value = mock_response

        service = NovaPoshtaDirectoryService()
        items = service.search_settlements('Київ', limit=5)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['label'], 'м. Київ, Київ')
        self.assertEqual(items[0]['settlement_ref'], 'settlement-ref')
        self.assertEqual(items[0]['city_ref'], 'delivery-city-ref')
        self.assertEqual(items[0]['warehouses'], 1473)

    @override_settings(NOVA_POSHTA_API_KEY='test-key')
    def test_search_settlements_maps_common_latin_kyiv_aliases(self):
        service = NovaPoshtaDirectoryService()
        settlement = {'label': 'м. Київ, Київ', 'settlement_ref': 's', 'city_ref': 'c'}

        with patch.object(
            service,
            '_search_settlements_uncached',
            return_value=[settlement],
        ) as mock_uncached:
            for query in ('Kyiv', 'Kiev', '  KYIV  ', 'Київ'):
                with self.subTest(query=query):
                    self.assertEqual(service.search_settlements(query, limit=5), [settlement])

        self.assertEqual(mock_uncached.call_count, 1)
        self.assertEqual(mock_uncached.call_args.args, ('Київ', 5))

    @override_settings(NOVA_POSHTA_API_KEY='test-key')
    def test_search_warehouses_uses_full_directory_for_text_query(self):
        service = NovaPoshtaDirectoryService()

        with (
            patch.object(service, '_get_warehouse_type_map', return_value={}),
            patch.object(service, '_request') as mock_request,
        ):
            mock_request.return_value = [
                {
                    'Ref': 'branch-ref',
                    'ShortAddress': 'Київ, вул. Петра Калнишевського, 2',
                    'Description': 'Відділення №7',
                    'CategoryOfWarehouse': 'Branch',
                    'Number': '7',
                },
                {
                    'Ref': 'postomat-ref-id',
                    'ShortAddress': 'Київ, вул. Петра Калнишевського, 2',
                    'Description': 'Пункт видачі',
                    'CategoryOfWarehouse': 'Postomat',
                    'Number': '22',
                },
            ]

            items = service.search_warehouses(
                settlement_ref='settlement-ref',
                city_ref='delivery-city-ref',
                query='Петра',
                kind='postomat',
                limit=20,
            )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['ref'], 'postomat-ref-id')
        self.assertEqual(items[0]['kind'], 'postomat')
        self.assertEqual(
            mock_request.call_args.args,
            ('Address', 'getWarehouses', {'CityRef': 'delivery-city-ref'}),
        )

    @override_settings(NOVA_POSHTA_API_KEY='test-key')
    def test_search_warehouses_ranks_exact_number_first_for_text_query(self):
        service = NovaPoshtaDirectoryService()

        with (
            patch.object(service, '_get_warehouse_type_map', return_value={}),
            patch.object(service, '_request') as mock_request,
        ):
            mock_request.return_value = [
                {
                    'Ref': 'street-match',
                    'ShortAddress': 'Київ, вул. Тестова, 22',
                    'Description': 'Відділення №7',
                    'CategoryOfWarehouse': 'Branch',
                    'Number': '7',
                },
                {
                    'Ref': 'exact-number',
                    'ShortAddress': 'Київ, вул. Інша, 1',
                    'Description': 'Відділення №22',
                    'CategoryOfWarehouse': 'Branch',
                    'Number': '22',
                },
            ]

            items = service.search_warehouses(
                settlement_ref='settlement-ref',
                city_ref='delivery-city-ref',
                query='22',
                kind='branch',
                limit=20,
            )

        self.assertEqual([item['ref'] for item in items[:2]], ['exact-number', 'street-match'])

    @override_settings(NOVA_POSHTA_API_KEY='test-key')
    def test_search_warehouses_uses_fast_postomat_lookup_without_query(self):
        service = NovaPoshtaDirectoryService()

        with (
            patch.object(service, '_get_warehouse_type_map', return_value={'postomat-type': 'Поштомат'}),
            patch.object(service, '_request') as mock_request,
        ):
            mock_request.return_value = [
                {
                    'Ref': 'postomat-ref-id',
                    'ShortAddress': 'Поштомат №22: ТЦ Тест',
                    'Description': 'Поштомат №22',
                    'TypeOfWarehouse': 'postomat-type',
                    'Number': '22',
                }
            ]

            items = service.search_warehouses(
                settlement_ref='settlement-ref',
                city_ref='delivery-city-ref',
                query='',
                kind='postomat',
                limit=20,
            )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['ref'], 'postomat-ref-id')
        self.assertEqual(
            mock_request.call_args.args,
            (
                'Address',
                'getWarehouses',
                {
                    'Page': '1',
                    'FindByString': 'поштомат',
                    'Limit': '50',
                    'CityRef': 'delivery-city-ref',
                },
            ),
        )

    @override_settings(NOVA_POSHTA_API_KEY='test-key')
    def test_search_warehouses_does_not_cache_empty_results(self):
        # Nova Poshta `getWarehouses` is eventually-consistent and intermittently
        # returns an empty list for a perfectly valid settlement. Caching that empty
        # response turned a momentary blip into a multi-minute outage, so empty
        # results must never be cached.
        service = NovaPoshtaDirectoryService()
        warehouse = {'ref': 'warehouse-ref', 'label': 'Відділення №1', 'kind': 'branch'}

        with patch.object(
            service,
            '_search_warehouses_uncached',
            side_effect=[[], [warehouse]],
        ) as mock_uncached:
            first = service.search_warehouses(city_ref='delivery-city-ref', kind='all', limit=10)
            second = service.search_warehouses(city_ref='delivery-city-ref', kind='all', limit=10)

        self.assertEqual(first, [])
        self.assertEqual(second, [warehouse])
        self.assertEqual(mock_uncached.call_count, 2)

    @override_settings(NOVA_POSHTA_API_KEY='test-key')
    def test_search_settlements_does_not_cache_empty_results(self):
        service = NovaPoshtaDirectoryService()
        settlement = {'label': 'м. Київ, Київ', 'settlement_ref': 's', 'city_ref': 'c'}

        with patch.object(
            service,
            '_search_settlements_uncached',
            side_effect=[[], [settlement]],
        ) as mock_uncached:
            first = service.search_settlements('Київ', limit=10)
            second = service.search_settlements('Київ', limit=10)

        self.assertEqual(first, [])
        self.assertEqual(second, [settlement])
        self.assertEqual(mock_uncached.call_count, 2)

    def test_resolve_delivery_selection_accepts_signed_city_and_warehouse(self):
        selection = resolve_delivery_selection(
            {
                'np_city_token': build_city_choice_token(
                    {
                        'label': 'м. Київ, Київ',
                        'settlement_ref': 'settlement-ref',
                        'city_ref': 'delivery-city-ref',
                    }
                ),
                'np_warehouse_token': build_warehouse_choice_token(
                    {
                        'label': 'Відділення №22, Київ, вул. Тестова, 1',
                        'ref': 'warehouse-ref',
                        'kind': 'branch',
                        'city_ref': 'delivery-city-ref',
                    }
                ),
            }
        )

        self.assertEqual(selection.city, 'м. Київ, Київ')
        self.assertEqual(selection.np_office, 'Відділення №22, Київ, вул. Тестова, 1')
        self.assertEqual(selection.city_ref, 'delivery-city-ref')
        self.assertEqual(selection.warehouse_ref, 'warehouse-ref')

    def test_resolve_delivery_selection_rejects_mismatched_city(self):
        with self.assertRaises(NovaPoshtaSelectionError) as exc_info:
            resolve_delivery_selection(
                {
                    'np_city_token': build_city_choice_token(
                        {
                            'label': 'м. Київ, Київ',
                            'settlement_ref': 'settlement-ref',
                            'city_ref': 'delivery-city-ref',
                        }
                    ),
                    'np_warehouse_token': build_warehouse_choice_token(
                        {
                            'label': 'Поштомат №22, Львів',
                            'ref': 'warehouse-ref-2',
                            'kind': 'postomat',
                            'city_ref': 'other-city-ref',
                        }
                    ),
                }
            )

        self.assertEqual(exc_info.exception.field, 'np_office')


@override_settings(NOVA_POSHTA_FALLBACK_ENABLED=False, RATELIMIT_ENABLE=False)
class NovaPoshtaLookupEndpointTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    @patch('storefront.views.cart.NovaPoshtaDirectoryService.search_settlements')
    def test_city_lookup_endpoint_returns_items(self, mock_search):
        mock_search.return_value = [
            {
                'label': 'м. Київ, Київ',
                'settlement_ref': 'settlement-ref',
                'city_ref': 'delivery-city-ref',
                'warehouses': 1473,
            }
        ]

        response = self.client.get(reverse('cart_np_city_search'), {'q': 'Київ'}, secure=True)
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            payload,
            {
                'ok': True,
                'items': [
                    {
                        'label': 'м. Київ, Київ',
                        'settlement_ref': 'settlement-ref',
                        'city_ref': 'delivery-city-ref',
                        'warehouses': 1473,
                        'token': payload['items'][0]['token'],
                    }
                ],
            },
        )
        mock_search.assert_called_once_with('Київ', limit=10)

    def test_warehouse_lookup_endpoint_requires_city_reference(self):
        response = self.client.get(reverse('cart_np_warehouse_search'), secure=True)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json(),
            {
                'ok': False,
                'error': 'Спочатку оберіть місто зі списку Нової пошти.',
            },
        )

    @patch('storefront.views.cart.NovaPoshtaDirectoryService.search_warehouses')
    def test_warehouse_lookup_endpoint_handles_unavailable_service(self, mock_search):
        mock_search.side_effect = NovaPoshtaLookupUnavailable(
            'Довідник Нової пошти тимчасово недоступний. Спробуйте ще раз трохи пізніше.'
        )

        response = self.client.get(
            reverse('cart_np_warehouse_search'),
            {
                'settlement_ref': 'settlement-ref',
                'kind': 'all',
            },
            secure=True,
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json(),
            {
                'ok': False,
                'available': False,
                'error': 'Довідник Нової пошти тимчасово недоступний. Спробуйте ще раз трохи пізніше.',
            },
        )


class _FakeItems:
    def __init__(self, items):
        self._items = list(items)

    def all(self):
        return list(self._items)

    def count(self):
        return len(self._items)


class NovaPoshtaWaybillServiceTests(SimpleTestCase):
    def _build_order(self, **overrides):
        item = SimpleNamespace(title='Худі TwoComms', qty=1)
        payload = {
            'pk': 101,
            'full_name': 'Тестовий клієнт',
            'phone': '+380991112233',
            'city': 'Київ',
            'np_office': 'Відділення №4',
            'np_settlement_ref': 'recipient-settlement-ref',
            'np_city_ref': 'recipient-city-ref',
            'np_warehouse_ref': 'recipient-warehouse-ref',
            'total_sum': '1499.00',
            'pay_type': 'prepay_200',
            'payment_status': 'unpaid',
            'items': _FakeItems([item]),
            'tracking_number': None,
            'nova_poshta_document_ref': None,
            'status': 'new',
        }
        payload.update(overrides)
        total_sum = Decimal(str(payload['total_sum']))
        payload.setdefault('get_remaining_amount', lambda total=total_sum: total - Decimal('200.00'))
        return SimpleNamespace(**payload)

    def test_build_waybill_description_uses_single_item_title(self):
        order = self._build_order()
        self.assertEqual(build_waybill_description(order), 'Одяг бренду TwoComms, Худі TwoComms')

    def test_build_waybill_description_uses_quantity_for_multiple_items(self):
        items = _FakeItems([
            SimpleNamespace(title='Футболка', qty=2),
            SimpleNamespace(title='Худі', qty=1),
        ])
        order = self._build_order(items=items)
        self.assertEqual(build_waybill_description(order), 'Одяг бренду TwoComms, у кількості 3 шт.')

    @patch.object(NovaPoshtaDocumentService, '_resolve_default_sender_point')
    def test_build_initial_payload_prefills_sender_and_prepay_cod(self, mock_sender_point):
        mock_sender_point.return_value = NovaPoshtaResolvedPoint(
            city_label='м. Харків, Харків',
            warehouse_label='Відділення №138 (до 200 кг): Проїзд Стадіонний, 13',
            settlement_ref='sender-settlement-ref',
            city_ref='sender-city-ref',
            warehouse_ref='sender-warehouse-ref',
            warehouse_kind='branch',
        )
        order = self._build_order()
        service = NovaPoshtaDocumentService()

        payload = service.build_initial_payload(order)

        self.assertEqual(payload['recipient_full_name'], 'Тестовий клієнт')
        self.assertEqual(payload['recipient_city_ref'], 'recipient-city-ref')
        self.assertEqual(payload['sender_city_ref'], 'sender-city-ref')
        self.assertEqual(payload['sender_warehouse_ref'], 'sender-warehouse-ref')
        self.assertEqual(payload['description'], 'Одяг бренду TwoComms, Худі TwoComms')
        self.assertEqual(payload['cod_amount'], '1299.00')
        self.assertTrue(payload['recipient_city_token'])
        self.assertTrue(payload['recipient_warehouse_token'])
        self.assertTrue(payload['sender_city_token'])
        self.assertTrue(payload['sender_warehouse_token'])
        self.assertEqual(payload['payer_type'], 'Recipient')
        self.assertEqual(payload['payment_method'], 'Cash')

    @patch.object(NovaPoshtaDocumentService, '_resolve_default_sender_point')
    def test_build_initial_payload_sets_full_cod_for_cod_orders(self, mock_sender_point):
        mock_sender_point.return_value = NovaPoshtaResolvedPoint(
            city_label='м. Харків, Харків',
            warehouse_label='Відділення №138',
            settlement_ref='sender-settlement-ref',
            city_ref='sender-city-ref',
            warehouse_ref='sender-warehouse-ref',
            warehouse_kind='branch',
        )
        order = self._build_order(pay_type='cod', total_sum='990.00')
        service = NovaPoshtaDocumentService()

        payload = service.build_initial_payload(order)

        self.assertEqual(payload['cod_amount'], '990.00')

    @patch.object(NovaPoshtaDocumentService, '_resolve_default_sender_point')
    def test_build_initial_payload_sets_zero_cod_for_fully_paid_prepay_order(self, mock_sender_point):
        mock_sender_point.return_value = NovaPoshtaResolvedPoint(
            city_label='м. Харків, Харків',
            warehouse_label='Відділення №138',
            settlement_ref='sender-settlement-ref',
            city_ref='sender-city-ref',
            warehouse_ref='sender-warehouse-ref',
            warehouse_kind='branch',
        )
        order = self._build_order(payment_status='paid')
        service = NovaPoshtaDocumentService()

        payload = service.build_initial_payload(order)

        self.assertEqual(payload['declared_cost'], '1499.00')
        self.assertEqual(payload['cod_amount'], '')

    def test_normalize_phone_accepts_common_ua_variants(self):
        self.assertEqual(normalize_phone('+38 (099) 111-22-33'), '+380991112233')
        self.assertEqual(normalize_phone('380991112233'), '+380991112233')
        self.assertEqual(normalize_phone('0991112233'), '+380991112233')
        self.assertEqual(normalize_phone('80991112233'), '+380991112233')

    @override_settings(NOVA_POSHTA_API_KEY='test-key')
    def test_delete_waybill_calls_internetdocument_delete(self):
        service = NovaPoshtaDocumentService()
        document_ref = '00000000-0000-0000-0000-000000000001'

        with patch.object(
            service,
            '_request',
            return_value={'data': [{'Ref': document_ref}], 'warnings': ['warning text']},
        ) as request_mock:
            result = service.delete_waybill(document_ref)

        request_mock.assert_called_once_with(
            'InternetDocument',
            'delete',
            {'DocumentRefs': document_ref},
        )
        self.assertEqual(result['document_ref'], document_ref)
        self.assertEqual(result['warnings'], ['warning text'])

    @override_settings(NOVA_POSHTA_API_KEY='test-key')
    def test_delete_waybill_requires_matching_api_confirmation(self):
        service = NovaPoshtaDocumentService()
        document_ref = '00000000-0000-0000-0000-000000000001'

        with patch.object(service, '_request', return_value={'data': [], 'warnings': []}):
            with self.assertRaisesRegex(NovaPoshtaDocumentError, 'не підтвердив видалення'):
                service.delete_waybill(document_ref)

        with patch.object(
            service,
            '_request',
            return_value={'data': [{'Ref': '00000000-0000-0000-0000-000000000002'}], 'warnings': []},
        ):
            with self.assertRaisesRegex(NovaPoshtaDocumentError, 'саме цієї накладної'):
                service.delete_waybill(document_ref)

    @override_settings(NOVA_POSHTA_API_KEY='test-key')
    def test_create_waybill_uses_afterpayment_without_backward_delivery(self):
        service = NovaPoshtaDocumentService()
        order = self._build_order(payment_status='prepaid')
        sender_point = NovaPoshtaResolvedPoint(
            city_label='м. Харків, Харків',
            warehouse_label='Відділення №138',
            settlement_ref='sender-settlement-ref',
            city_ref='sender-city-ref',
            warehouse_ref='sender-warehouse-ref',
            warehouse_kind='branch',
        )
        recipient_point = NovaPoshtaResolvedPoint(
            city_label='м. Київ, Київ',
            warehouse_label='Відділення №4',
            settlement_ref='recipient-settlement-ref',
            city_ref='recipient-city-ref',
            warehouse_ref='recipient-warehouse-ref',
            warehouse_kind='branch',
        )
        payload = {
            'recipient_full_name': 'Тестовий клієнт',
            'recipient_phone': '+380991112233',
            'recipient_city': 'Київ',
            'recipient_city_ref': 'recipient-city-ref',
            'recipient_warehouse': 'Відділення №4',
            'recipient_warehouse_ref': 'recipient-warehouse-ref',
            'sender_city': 'Харків',
            'sender_city_ref': 'sender-city-ref',
            'sender_warehouse': 'Відділення №138',
            'sender_warehouse_ref': 'sender-warehouse-ref',
            'description': 'Одяг бренду TwoComms',
            'declared_cost': '1499.00',
            'weight': '1',
            'seats_amount': '1',
            'length_cm': '30',
            'width_cm': '20',
            'height_cm': '8',
            'cod_amount': '1299.00',
            'payer_type': 'Recipient',
            'payment_method': 'Cash',
        }

        with (
            patch.object(service, '_resolve_sender_profile', return_value={
                'sender_ref': 'sender-ref',
                'contact_ref': 'sender-contact-ref',
                'phone': '380991112233',
            }),
            patch.object(service, '_resolve_point', side_effect=[sender_point, recipient_point]),
            patch.object(service, '_request') as request_mock,
        ):
            request_mock.side_effect = [
                {'data': [{'Ref': 'recipient-ref', 'ContactPerson': [{'Ref': 'recipient-contact-ref'}]}]},
                {'data': [{'IntDocNumber': '20451234123456', 'Ref': 'document-ref'}], 'warnings': []},
            ]
            result = service.create_waybill(order, payload)

        self.assertEqual(result['tracking_number'], '20451234123456')
        self.assertEqual(result['recipient_contact_ref'], 'recipient-contact-ref')
        self.assertEqual(request_mock.call_count, 2)
        model_name, called_method, method_properties = request_mock.call_args_list[-1].args
        self.assertEqual((model_name, called_method), ('InternetDocument', 'save'))
        self.assertEqual(method_properties['AfterpaymentOnGoodsCost'], '1299')
        self.assertNotIn('BackwardDeliveryData', method_properties)

    @override_settings(NOVA_POSHTA_API_KEY='test-key')
    def test_create_waybill_requires_document_ref(self):
        service = NovaPoshtaDocumentService()
        order = self._build_order(payment_status='paid')
        sender_point = NovaPoshtaResolvedPoint(
            city_label='м. Харків, Харків',
            warehouse_label='Відділення №138',
            settlement_ref='sender-settlement-ref',
            city_ref='sender-city-ref',
            warehouse_ref='sender-warehouse-ref',
            warehouse_kind='branch',
        )
        recipient_point = NovaPoshtaResolvedPoint(
            city_label='м. Київ, Київ',
            warehouse_label='Відділення №4',
            settlement_ref='recipient-settlement-ref',
            city_ref='recipient-city-ref',
            warehouse_ref='recipient-warehouse-ref',
            warehouse_kind='branch',
        )
        payload = {
            'recipient_full_name': 'Тестовий клієнт',
            'recipient_phone': '+380991112233',
            'recipient_city': 'Київ',
            'recipient_city_ref': 'recipient-city-ref',
            'recipient_warehouse': 'Відділення №4',
            'recipient_warehouse_ref': 'recipient-warehouse-ref',
            'sender_city': 'Харків',
            'sender_city_ref': 'sender-city-ref',
            'sender_warehouse': 'Відділення №138',
            'sender_warehouse_ref': 'sender-warehouse-ref',
            'description': 'Одяг бренду TwoComms',
            'declared_cost': '1499.00',
            'weight': '1',
            'seats_amount': '1',
            'length_cm': '30',
            'width_cm': '20',
            'height_cm': '8',
            'cod_amount': '',
            'payer_type': 'Recipient',
            'payment_method': 'Cash',
        }

        with (
            patch.object(service, '_resolve_sender_profile', return_value={
                'sender_ref': 'sender-ref',
                'contact_ref': 'sender-contact-ref',
                'phone': '380991112233',
            }),
            patch.object(service, '_resolve_point', side_effect=[sender_point, recipient_point]),
            patch.object(service, '_request') as request_mock,
        ):
            request_mock.side_effect = [
                {'data': [{'Ref': 'recipient-ref', 'ContactPerson': [{'Ref': 'recipient-contact-ref'}]}]},
                {'data': [{'IntDocNumber': '20451234123456'}], 'warnings': []},
            ]
            with self.assertRaisesRegex(NovaPoshtaDocumentError, 'Ref створеної накладної'):
                service.create_waybill(order, payload)

    @override_settings(NOVA_POSHTA_API_KEY='test-key')
    def test_create_waybill_deletes_incomplete_document_when_number_is_missing(self):
        service = NovaPoshtaDocumentService()
        order = self._build_order(payment_status='paid')
        sender_point = NovaPoshtaResolvedPoint(
            city_label='м. Харків, Харків',
            warehouse_label='Відділення №138',
            settlement_ref='sender-settlement-ref',
            city_ref='sender-city-ref',
            warehouse_ref='sender-warehouse-ref',
            warehouse_kind='branch',
        )
        recipient_point = NovaPoshtaResolvedPoint(
            city_label='м. Київ, Київ',
            warehouse_label='Відділення №4',
            settlement_ref='recipient-settlement-ref',
            city_ref='recipient-city-ref',
            warehouse_ref='recipient-warehouse-ref',
            warehouse_kind='branch',
        )
        payload = {
            'recipient_full_name': 'Тестовий клієнт',
            'recipient_phone': '+380991112233',
            'recipient_city': 'Київ',
            'recipient_city_ref': 'recipient-city-ref',
            'recipient_warehouse': 'Відділення №4',
            'recipient_warehouse_ref': 'recipient-warehouse-ref',
            'sender_city': 'Харків',
            'sender_city_ref': 'sender-city-ref',
            'sender_warehouse': 'Відділення №138',
            'sender_warehouse_ref': 'sender-warehouse-ref',
            'description': 'Одяг бренду TwoComms',
            'declared_cost': '1499.00',
            'weight': '1',
            'seats_amount': '1',
            'length_cm': '30',
            'width_cm': '20',
            'height_cm': '8',
            'cod_amount': '',
            'payer_type': 'Recipient',
            'payment_method': 'Cash',
        }

        with (
            patch.object(service, '_resolve_sender_profile', return_value={
                'sender_ref': 'sender-ref',
                'contact_ref': 'sender-contact-ref',
                'phone': '380991112233',
            }),
            patch.object(service, '_resolve_point', side_effect=[sender_point, recipient_point]),
            patch.object(service, '_request') as request_mock,
        ):
            request_mock.side_effect = [
                {'data': [{'Ref': 'recipient-ref', 'ContactPerson': [{'Ref': 'recipient-contact-ref'}]}]},
                {'data': [{'Ref': 'document-ref'}], 'warnings': []},
                {'data': [{'Ref': 'document-ref'}], 'warnings': []},
            ]
            with self.assertRaisesRegex(NovaPoshtaDocumentError, 'не повернув номер ТТН'):
                service.create_waybill(order, payload)

        self.assertEqual(request_mock.call_args_list[-1].args, (
            'InternetDocument',
            'delete',
            {'DocumentRefs': 'document-ref'},
        ))


class NovaPoshtaWaybillFormTests(SimpleTestCase):
    def test_form_normalizes_recipient_phone(self):
        form = TelegramNovaPoshtaWaybillForm(
            data={
                'recipient_full_name': 'Тест Клієнт',
                'recipient_phone': '+38 (099) 111-22-33',
                'recipient_city': 'Київ',
                'recipient_settlement_ref': '',
                'recipient_city_ref': '',
                'recipient_warehouse': 'Відділення №4',
                'recipient_warehouse_ref': '',
                'sender_city': 'Харків',
                'sender_settlement_ref': '',
                'sender_city_ref': '',
                'sender_warehouse': 'Відділення №138',
                'sender_warehouse_ref': '',
                'description': 'Одяг бренду TwoComms',
                'declared_cost': '1499',
                'weight': '1',
                'seats_amount': '1',
                'length_cm': '30',
                'width_cm': '20',
                'height_cm': '8',
                'cod_amount': '',
                'payer_type': 'Recipient',
                'payment_method': 'Cash',
            }
        )

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['recipient_phone'], '+380991112233')
        self.assertEqual(form.cleaned_data['cod_amount'], 0)

    def test_form_accepts_long_signed_selector_tokens(self):
        warehouse_token = build_warehouse_choice_token(
            {
                'label': 'Поштомат №12345: Київ, вул. Тестова дуже довга назва відділення до 200 кг, 123',
                'ref': '00000000-0000-0000-0000-000000000002',
                'kind': 'postomat',
                'city_ref': '00000000-0000-0000-0000-000000000001',
            }
        )
        self.assertGreater(len(warehouse_token), 255)

        form = TelegramNovaPoshtaWaybillForm(
            data={
                'recipient_full_name': 'Тест Клієнт',
                'recipient_phone': '+38 (099) 111-22-33',
                'recipient_city': 'Київ',
                'recipient_settlement_ref': '00000000-0000-0000-0000-000000000001',
                'recipient_city_ref': '00000000-0000-0000-0000-000000000001',
                'recipient_city_token': build_city_choice_token(
                    {
                        'label': 'Київ',
                        'settlement_ref': '00000000-0000-0000-0000-000000000001',
                        'city_ref': '00000000-0000-0000-0000-000000000001',
                    }
                ),
                'recipient_warehouse': 'Поштомат №12345',
                'recipient_warehouse_ref': '00000000-0000-0000-0000-000000000002',
                'recipient_warehouse_token': warehouse_token,
                'sender_city': 'Харків',
                'sender_settlement_ref': '00000000-0000-0000-0000-000000000003',
                'sender_city_ref': '00000000-0000-0000-0000-000000000003',
                'sender_city_token': build_city_choice_token(
                    {
                        'label': 'Харків',
                        'settlement_ref': '00000000-0000-0000-0000-000000000003',
                        'city_ref': '00000000-0000-0000-0000-000000000003',
                    }
                ),
                'sender_warehouse': 'Відділення №138',
                'sender_warehouse_ref': '00000000-0000-0000-0000-000000000004',
                'sender_warehouse_token': warehouse_token,
                'description': 'Одяг бренду TwoComms',
                'declared_cost': '1499',
                'weight': '1',
                'seats_amount': '1',
                'length_cm': '30',
                'width_cm': '20',
                'height_cm': '8',
                'cod_amount': '',
                'payer_type': 'Recipient',
                'payment_method': 'Cash',
            }
        )

        self.assertTrue(form.is_valid(), form.errors.as_json())


@override_settings(ROOT_URLCONF='storefront.urls', SITE_BASE_URL='https://twocomms.shop')
class TelegramOrderActionLinkTests(SimpleTestCase):
    def test_generic_action_token_roundtrip(self):
        token = build_order_action_token(77, TELEGRAM_CREATE_NP_WAYBILL_ACTION)
        self.assertTrue(
            verify_order_action_token(
                token,
                order_id=77,
                action=TELEGRAM_CREATE_NP_WAYBILL_ACTION,
            )
        )
        self.assertFalse(
            verify_order_action_token(
                token,
                order_id=77,
                action='ship',
            )
        )

    def test_build_waybill_action_url_uses_new_route(self):
        order = SimpleNamespace(pk=33)
        url = build_order_action_url(
            order,
            TELEGRAM_CREATE_NP_WAYBILL_ACTION,
            route_name='telegram_order_np_waybill_action',
        )

        self.assertIn('/orders/telegram-waybill/33/create-np-waybill/', url)
        self.assertIn('token=', url)

    @patch('orders.telegram_notifications.build_order_status_action_url', return_value='https://example.com/ship')
    @patch('orders.telegram_notifications.build_order_action_url', return_value='https://example.com/create')
    def test_order_notification_markup_contains_create_waybill_and_ship_buttons(self, *_mocks):
        order = SimpleNamespace(
            pk=1,
            status='new',
            tracking_number='',
            nova_poshta_document_ref='',
        )
        notifier = TelegramNotifier(bot_token='token', admin_id='1', async_enabled=False)

        markup = notifier._build_order_management_reply_markup(order)

        self.assertEqual(markup['inline_keyboard'][0][0]['text'], '📦 Створити ТТН НП')
        self.assertEqual(markup['inline_keyboard'][0][0]['url'], 'https://example.com/create')
        self.assertEqual(markup['inline_keyboard'][1][0]['text'], '🚚 Відправлено + ТТН')
        self.assertEqual(markup['inline_keyboard'][1][0]['url'], 'https://example.com/ship')

    @patch('orders.telegram_notifications.build_order_action_url', return_value='https://example.com/delete')
    def test_order_notification_markup_contains_delete_waybill_button(self, _build_action_url):
        order = SimpleNamespace(
            pk=1,
            status='ship',
            tracking_number='20451234123456',
            nova_poshta_document_ref='00000000-0000-0000-0000-000000000001',
        )
        notifier = TelegramNotifier(bot_token='token', admin_id='1', async_enabled=False)

        markup = notifier._build_order_management_reply_markup(order)

        self.assertEqual(markup['inline_keyboard'][0][0]['text'], '🗑 Видалити ТТН НП')
        self.assertEqual(markup['inline_keyboard'][0][0]['url'], 'https://example.com/delete')
        _build_action_url.assert_called_once_with(
            order,
            TELEGRAM_DELETE_NP_WAYBILL_ACTION,
            route_name='telegram_order_np_waybill_action',
            token_scope='00000000-0000-0000-0000-000000000001',
        )


class PickWarehouseCandidateTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    @override_settings(NOVA_POSHTA_API_KEY='test-key')
    def test_falls_back_to_known_ref_when_directory_empty(self):
        # When the order already carries a validated warehouse Ref, a flaky/empty
        # directory response must not block TTN creation.
        service = NovaPoshtaDocumentService()

        with patch.object(service.directory, 'search_warehouses', return_value=[]):
            result = service._pick_warehouse_candidate(
                'Санжійка, Приморська, 38',
                settlement_ref='settlement-ref',
                city_ref='city-ref',
                warehouse_ref='known-warehouse-ref',
                preferred_kind='all',
                point_role='одержувача',
            )

        self.assertEqual(result['ref'], 'known-warehouse-ref')
        self.assertEqual(result['city_ref'], 'city-ref')
        self.assertEqual(result['kind'], 'branch')
        self.assertEqual(result['label'], 'Санжійка, Приморська, 38')

    @override_settings(NOVA_POSHTA_API_KEY='test-key')
    def test_known_ref_fallback_infers_postomat_kind(self):
        service = NovaPoshtaDocumentService()

        with patch.object(service.directory, 'search_warehouses', return_value=[]):
            result = service._pick_warehouse_candidate(
                'Поштомат №12345: Київ',
                settlement_ref='settlement-ref',
                city_ref='city-ref',
                warehouse_ref='known-postomat-ref',
                preferred_kind='all',
                point_role='одержувача',
            )

        self.assertEqual(result['ref'], 'known-postomat-ref')
        self.assertEqual(result['kind'], 'postomat')

    @override_settings(NOVA_POSHTA_API_KEY='test-key')
    def test_prefers_directory_item_matching_known_ref(self):
        service = NovaPoshtaDocumentService()
        directory_item = {
            'ref': 'known-warehouse-ref',
            'label': 'Санжійка, Приморська, 38',
            'kind': 'branch',
            'number': '1',
            'city_ref': 'city-ref',
        }

        with patch.object(service.directory, 'search_warehouses', return_value=[directory_item]):
            result = service._pick_warehouse_candidate(
                'Санжійка, Приморська, 38',
                settlement_ref='settlement-ref',
                city_ref='city-ref',
                warehouse_ref='known-warehouse-ref',
                preferred_kind='all',
                point_role='одержувача',
            )

        self.assertIs(result, directory_item)

    @override_settings(NOVA_POSHTA_API_KEY='test-key')
    def test_still_raises_when_no_ref_and_directory_empty(self):
        service = NovaPoshtaDocumentService()

        with patch.object(service.directory, 'search_warehouses', return_value=[]):
            with self.assertRaisesRegex(NovaPoshtaDocumentError, 'Не вдалося знайти'):
                service._pick_warehouse_candidate(
                    'Відділення №4',
                    settlement_ref='settlement-ref',
                    city_ref='city-ref',
                    warehouse_ref='',
                    preferred_kind='all',
                    point_role='одержувача',
                )
