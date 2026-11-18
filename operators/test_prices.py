from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from .aggregator import Aggregator
from .models import AggregatedProduct, OperatorPrice, PriceList, Product, ProductPrice, Supplier
from .parser import SmartParser, TextPriceParser


class MoneyParsingTests(SimpleTestCase):
    def test_text_money_preserves_product_fields(self):
        for money in ('49 500', '49500.00', '49500', '49\xa0500 ₽',
                      '49500₽', '49500 ₽', '49 500,25 ₽', '49500.25'):
            with self.subTest(money=money):
                products = TextPriceParser(f'iPhone 15\n15 128 blue 🇮🇳 {money}').parse()
                self.assertEqual(len(products), 1)
                product = products[0]
                self.assertEqual(product['model'], '15')
                self.assertEqual(product['memory'], '128')
                self.assertEqual(product['color'], 'Blue')
                self.assertEqual(product['region'], 'India')
                self.assertIsInstance(product['price'], Decimal)
                self.assertEqual(product['price'], Decimal('49500.25' if '25' in money else '49500'))

    def test_model_and_storage_are_not_prices(self):
        for line in ('iPhone 15', '16 ₽', 'SE 2 40', 'iPhone 15 128',
                     'MacBook Air M4 16/256', 'iPhone 15 blue 49500oops'):
            with self.subTest(line=line):
                self.assertIsNone(TextPriceParser('').extract_price(line)[0])

    def test_numeric_storage_stays_in_payload(self):
        price, rest = TextPriceParser('').extract_price('iPhone 15 128 49500.00 ₽')
        self.assertEqual(price, Decimal('49500'))
        self.assertEqual(rest, 'iPhone 15 128')

    def test_spreadsheet_money_uses_decimal(self):
        parser = SmartParser('unused.csv')
        for value in (49500, 49500.25, Decimal('49500.25'), '49 500,25 ₽'):
            with self.subTest(value=value):
                price = parser.normalize_price(value)
                self.assertIsInstance(price, Decimal)
                self.assertEqual(price, Decimal(str(value)) if not isinstance(value, str) else Decimal('49500.25'))


class LatestSnapshotTests(TestCase):
    def setUp(self):
        self.supplier = Supplier.objects.create(name='Supplier')

    def pricelist(self, status='processed', supplier=None, uploaded_at=None):
        pricelist = PriceList.objects.create(supplier=supplier or self.supplier, status=status)
        if uploaded_at is not None:
            PriceList.objects.filter(pk=pricelist.pk).update(uploaded_at=uploaded_at)
        return pricelist

    def product(self, pricelist, price='49500', model='15'):
        return Product.objects.create(
            supplier=pricelist.supplier, pricelist=pricelist, category='phone',
            brand='iPhone', model=model, memory='128', price=Decimal(price),
        )

    def aggregate(self):
        result = Aggregator.aggregate_all()
        self.assertEqual(result['errors'], 0, result)
        return result

    def test_latest_success_replaces_entire_supplier_snapshot(self):
        old = self.pricelist()
        self.product(old, '40000')
        removed = self.product(old, model='14')
        self.aggregate()
        catalog = AggregatedProduct.objects.get(model='14')
        manual = OperatorPrice.objects.create(aggregated_product=catalog, price='55000')
        latest = self.pricelist()
        chosen = self.product(latest, '50000')
        self.product(latest, '51000')
        for status in ('new', 'processing', 'error'):
            self.product(self.pricelist(status=status), '10000', model='16')
        other = Supplier.objects.create(name='Other')
        other_product = self.product(self.pricelist(supplier=other), '49000')

        for _ in range(2):
            self.assertEqual(self.aggregate()['aggregated'], 1)
            self.assertCountEqual(ProductPrice.objects.values_list('product_id', flat=True),
                                  [chosen.pk, other_product.pk])
            self.assertFalse(ProductPrice.objects.filter(product=removed).exists())
            manual.refresh_from_db()
            self.assertEqual(manual.aggregated_product_id, catalog.pk)
            self.assertEqual(manual.price, Decimal('55000'))

    def test_empty_latest_snapshot_does_not_resurrect_old_products(self):
        self.product(self.pricelist())
        self.aggregate()
        self.pricelist()
        self.assertEqual(self.aggregate()['aggregated'], 0)
        self.assertFalse(ProductPrice.objects.exists())

    def test_upload_time_then_id_choose_latest_and_exclude_yesterday(self):
        now = timezone.now()
        chosen = self.product(self.pricelist(uploaded_at=now), '50000')
        self.product(self.pricelist(uploaded_at=now - timedelta(minutes=1)), '40000')
        self.product(self.pricelist(uploaded_at=now - timedelta(days=1)), model='14')
        self.aggregate()
        self.assertEqual(ProductPrice.objects.get().product_id, chosen.pk)
        tied = self.product(self.pricelist(uploaded_at=now), '52000')
        self.aggregate()
        self.assertEqual(ProductPrice.objects.get().product_id, tied.pk)

    def test_failed_rebuild_rolls_back_existing_supplier_prices(self):
        product = self.product(self.pricelist())
        self.aggregate()
        with patch('operators.aggregator.ProductPrice.objects.create', side_effect=RuntimeError('failed')):
            self.assertEqual(Aggregator.aggregate_all()['errors'], 1)
        self.assertEqual(ProductPrice.objects.get().product_id, product.pk)
