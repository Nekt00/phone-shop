from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from operators.models import (
    AggregatedProduct, OperatorPrice, PriceList, Product, ProductPrice,
    Supplier, TelegramCustomer, TelegramCustomerMessage, TelegramCustomerRequest,
)


class Command(BaseCommand):
    help = 'Create fictional portfolio data in an empty database. Sends no Telegram messages.'

    @transaction.atomic
    def handle(self, *args, **options):
        if AggregatedProduct.objects.exists() or Supplier.objects.exists() or TelegramCustomer.objects.exists():
            raise CommandError('Demo data requires an empty catalog, suppliers and Telegram customers.')

        suppliers = [Supplier.objects.create(name=name) for name in ['Техно Опт', 'Мобильный склад', 'Гаджет Поставка']]
        lists = [PriceList.objects.create(supplier=supplier, status='processed') for supplier in suppliers]
        rows = [
            ('phone', 'iPhone', '17', '256', 'black', 79500),
            ('phone', 'iPhone', '17', '256', 'blue', 81000),
            ('phone', 'iPhone', '17 Pro', '256', 'silver', 109000),
            ('phone', 'iPhone', '17 Pro Max', '512', 'silver', 135000),
            ('phone', 'iPhone', '16', '128', 'black', 59500),
            ('phone', 'Samsung', 'Galaxy S25', '256', 'blue', 66000),
            ('phone', 'Samsung', 'Galaxy S25 Ultra', '512', 'black', 94500),
            ('tablet', 'iPad', 'Air M3', '128', 'blue', 57000),
            ('accessory', 'AirPods', 'AirPods Pro 2', '', 'white', 17500),
            ('laptop', 'MacBook', 'Air M4', '256', 'silver', 89000),
        ]
        for category, brand, model, memory, color, price in rows:
            fields = dict(category=category, brand=brand, model=model, memory=memory, color=color)
            aggregated = AggregatedProduct.objects.create(**fields)
            for index, (supplier, pricelist) in enumerate(zip(suppliers, lists)):
                value = Decimal(price + index * 1500)
                product = Product.objects.create(supplier=supplier, pricelist=pricelist, price=value, **fields)
                ProductPrice.objects.create(aggregated_product=aggregated, supplier=supplier, product=product, price=value)
            OperatorPrice.objects.create(aggregated_product=aggregated, price=price + 5000, source_supplier=suppliers[0])

        customer = TelegramCustomer.objects.create(telegram_id=100000001, first_name='Демо-клиент', username='demo_customer')
        request = TelegramCustomerRequest.objects.create(
            customer=customer, request_type='availability', client_message='Здравствуйте! iPhone 17 256 ГБ в синем цвете есть в наличии?',
            product_query='iPhone 17 256 blue',
        )
        TelegramCustomerMessage.objects.create(request=request, sender_type='customer', text=request.client_message)
        self.stdout.write(self.style.SUCCESS('Created 10 models, 3 suppliers and 1 fictional request. No messages sent.'))
