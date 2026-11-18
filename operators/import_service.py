from django.db import transaction

from .aggregator import Aggregator
from .models import PriceList, Product


def _save_products(pricelist, rows):
    if not rows:
        raise ValueError('Не найдено товаров в прайс-листе')
    products = []
    for row in rows:
        product = Product(
            supplier=pricelist.supplier, pricelist=pricelist,
            **{field: row.get(field, '') for field in ('category', 'brand', 'model', 'color', 'memory', 'region', 'sim_type', 'specs')},
            price=row['price'],
        )
        product.full_clean()
        if product.price <= 0:
            raise ValueError('Цена должна быть положительной')
        products.append(product)
    Product.objects.filter(pricelist=pricelist).delete()
    Product.objects.bulk_create(products)
    pricelist.status = 'processed'
    pricelist.save(update_fields=['status'])
    result = Aggregator.aggregate_all()
    if result['errors']:
        raise ValueError(result.get('error', 'Ошибка агрегации'))
    return len(products)


@transaction.atomic
def import_saved_file(pricelist_id):
    from .parser import SmartParser

    pricelist = PriceList.objects.select_for_update().get(pk=pricelist_id)
    if pricelist.status == 'processed':
        return 0
    return _save_products(pricelist, SmartParser(pricelist.file.path).parse())


@transaction.atomic
def import_text(supplier, text, user):
    from .parser import TextPriceParser

    rows = TextPriceParser(text).parse()
    pricelist = PriceList.objects.create(supplier=supplier, uploaded_by=user)
    return _save_products(pricelist, rows)
