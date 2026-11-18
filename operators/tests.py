from django.test import TestCase
from django.contrib.auth import get_user_model
from django.urls import reverse
from unittest.mock import patch

from .models import AggregatedProduct, OperatorPrice, PriceList, Product, ProductPrice, Supplier
from .aggregator import Aggregator
from .parser import TextPriceParser


class TextPriceParserTests(TestCase):
    def test_base_ipad_keeps_explicit_model_name(self):
        parsed = TextPriceParser('iPad 13 128 WiFi 50000').parse()

        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]['category'], 'tablet')
        self.assertEqual(parsed[0]['brand'], 'iPad')
        self.assertEqual(parsed[0]['model'], 'iPad 13')
        self.assertEqual(parsed[0]['memory'], '128')
        self.assertEqual(parsed[0]['specs'], 'WiFi')

    def test_iphone_header_is_not_parsed_as_product_with_price(self):
        parsed = TextPriceParser('iPhone 15\n16 ₽\n15 128 blue 🇮🇳 49500').parse()

        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]['brand'], 'iPhone')
        self.assertEqual(parsed[0]['model'], '15')
        self.assertEqual(parsed[0]['price'], 49500.0)


class AggregatedProductDisplayTests(TestCase):
    def test_display_name_does_not_duplicate_brand_when_model_contains_it(self):
        product = AggregatedProduct(
            category='tablet',
            brand='iPad',
            model='iPad 13',
            memory='128',
            specs='WiFi',
        )

        self.assertEqual(str(product), 'iPad 13 128gb WiFi')


class AggregationPreservationTests(TestCase):
    def test_reaggregation_keeps_manual_prices_and_product_identity(self):
        supplier = Supplier.objects.create(name='Demo supplier')
        pricelist = PriceList.objects.create(supplier=supplier, status='processed')
        fields = dict(category='phone', brand='iPhone', model='17', memory='256', color='black')
        catalog_product = AggregatedProduct.objects.create(**fields)
        manual = OperatorPrice.objects.create(aggregated_product=catalog_product, price=85000)
        Product.objects.create(supplier=supplier, pricelist=pricelist, price=80000, **fields)
        manual_only = AggregatedProduct.objects.create(brand='Samsung', model='S25')
        OperatorPrice.objects.create(aggregated_product=manual_only, price=70000)

        for _ in range(2):
            result = Aggregator.aggregate_all()
            self.assertEqual(result['errors'], 0)
            manual.refresh_from_db()
            self.assertEqual(manual.aggregated_product_id, catalog_product.pk)
            self.assertEqual(manual.price, 85000)
            self.assertTrue(OperatorPrice.objects.filter(aggregated_product=manual_only).exists())
            self.assertEqual(ProductPrice.objects.get(aggregated_product=catalog_product).price, 80000)
            self.assertEqual(AggregatedProduct.objects.count(), 2)


class ReleaseSafetyTests(TestCase):
    @patch('operators.views.request_cleanup')
    def test_healthcheck_does_not_call_telegram_cleanup(self, cleanup):
        self.assertEqual(self.client.get('/healthz/').status_code, 200)
        cleanup.assert_not_called()

    def test_operator_cannot_read_debug_endpoints(self):
        user = get_user_model().objects.create_user(username='operator')
        self.client.force_login(user)
        for name in ['env_debug', 'telegram_debug_log']:
            self.assertEqual(self.client.get(reverse('operators:' + name)).status_code, 302)

    @patch('operators.publication_service.send_telegram_messages', side_effect=ValueError('network failed'))
    @patch('operators.publication_service.delete_telegram_messages')
    def test_failed_publication_keeps_old_pricelist(self, delete, send):
        from .models import TelegramPublication
        publication = TelegramPublication.objects.create(kind='price', text='Old', message_ids=[1])
        user = get_user_model().objects.create_user(username='publisher')
        self.client.force_login(user)
        self.client.post(reverse('operators:send_telegram_pricelist'), {'telegram_text': 'New'})
        publication.refresh_from_db()
        self.assertTrue(publication.is_active)
        delete.assert_not_called()
