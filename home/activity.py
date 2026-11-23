from .models import OrderPaymentEvent, OrderTrackingEvent


def user_display_name(user):
    return user.get_full_name().strip() or user.get_username()


def record_order_status_change(
    order, previous_status, updated_by, description=None
):
    return OrderTrackingEvent.objects.create(
        order=order,
        previous_status=previous_status,
        status=order.status,
        description=(
            description
            or f"Your order is now {order.get_status_display().lower()}."
        ),
        updated_by_name=user_display_name(updated_by) if updated_by else "",
        updated_by=updated_by,
    )


def record_payment_status_change(order, previous_status, new_status, updated_by):
    return OrderPaymentEvent.objects.create(
        order=order,
        previous_status=previous_status,
        new_status=new_status,
        updated_by_name=user_display_name(updated_by),
        updated_by=updated_by,
    )
