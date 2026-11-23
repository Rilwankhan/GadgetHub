from django.db.models import F

from .models import Order, Product


class StockUnavailable(Exception):
    pass


def reserve_products(quantities):
    product_ids = sorted(quantities)
    products = {
        product.pk: product
        for product in Product.objects.select_for_update()
        .filter(pk__in=product_ids, is_active=True)
        .order_by("pk")
    }
    if len(products) != len(product_ids):
        raise StockUnavailable(
            "One or more products are no longer available."
        )

    for product_id in product_ids:
        product = products[product_id]
        quantity = quantities[product_id]
        if product.stock_quantity < quantity:
            raise StockUnavailable(
                f"Only {product.stock_quantity} {product.name} available."
            )

    for product_id in product_ids:
        product = products[product_id]
        quantity = quantities[product_id]
        updated = Product.objects.filter(
            pk=product_id,
            is_active=True,
            stock_quantity__gte=quantity,
        ).update(stock_quantity=F("stock_quantity") - quantity)
        if updated != 1:
            raise StockUnavailable(
                f"Not enough {product.name} is currently available."
            )
        product.stock_quantity -= quantity

    return products


def order_product_quantities(order):
    quantities = {}
    has_missing_product = False
    for product_id, quantity in order.items.values_list("product_id", "quantity"):
        if product_id is None:
            has_missing_product = True
            continue
        quantities[product_id] = quantities.get(product_id, 0) + quantity
    return quantities, has_missing_product


def reserve_order_stock(order):
    if order.stock_deducted:
        return
    quantities, has_missing_product = order_product_quantities(order)
    if has_missing_product or not quantities:
        raise StockUnavailable("Order items are no longer available.")
    reserve_products(quantities)
    order.stock_deducted = True
    order.save(update_fields=["stock_deducted"])


def order_stock_available(order):
    if order.stock_deducted:
        return True

    quantities, has_missing_product = order_product_quantities(order)
    if has_missing_product or not quantities:
        return False

    products = {
        product_id: (stock_quantity, is_active)
        for product_id, stock_quantity, is_active in Product.objects.filter(
            pk__in=quantities
        ).values_list("pk", "stock_quantity", "is_active")
    }
    return len(products) == len(quantities) and all(
        products[product_id][1]
        and products[product_id][0] >= quantity
        for product_id, quantity in quantities.items()
    )


def restore_order_stock(order):
    if not order.stock_deducted:
        return False

    quantities, _ = order_product_quantities(order)
    products = (
        Product.objects.select_for_update()
        .filter(pk__in=quantities)
        .order_by("pk")
    )
    for product in products:
        Product.objects.filter(pk=product.pk).update(
            stock_quantity=F("stock_quantity") + quantities[product.pk]
        )

    order.stock_deducted = False
    order.save(update_fields=["stock_deducted"])
    return True
