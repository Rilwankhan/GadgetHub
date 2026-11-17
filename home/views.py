import json
import math
import re
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth import login, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm, UserCreationForm
from django.contrib.auth.views import LoginView
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from .forms import AdminProfileForm, CustomerProfileForm
from .models import (
    Category,
    CustomerProfile,
    Order,
    OrderItem,
    OrderTrackingEvent,
    Product,
)


def role_dashboard_url(user):
    return reverse("admin:index") if user.is_staff else reverse("account")


class SharedLoginView(LoginView):
    template_name = "home/login.html"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect(role_dashboard_url(request.user))
        return super().dispatch(request, *args, **kwargs)

    def get_success_url(self):
        return role_dashboard_url(self.request.user)


def home(request):
    products = list(
        Product.objects.filter(is_active=True)
        .select_related("category")
        .values(
            "id", "name", "category__name", "price", "badge", "description", "image"
        )
    )
    for product in products:
        product["category"] = product.pop("category__name")
        product["price"] = float(product["price"])
    categories = Category.objects.filter(products__is_active=True).distinct()
    return render(
        request, "home/index.html", {"products": products, "categories": categories}
    )


def register(request):
    if request.user.is_authenticated:
        return redirect(role_dashboard_url(request.user))
    form = UserCreationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        CustomerProfile.objects.get_or_create(user=user)
        login(request, user)
        return redirect("account")
    return render(request, "home/register.html", {"form": form})


@login_required
def account(request):
    profile, _ = CustomerProfile.objects.get_or_create(user=request.user)
    is_admin = request.user.is_staff
    profile_form_class = AdminProfileForm if is_admin else CustomerProfileForm
    profile_form_kwargs = (
        {"user": request.user, "profile": profile}
        if is_admin
        else {"instance": profile, "user": request.user}
    )
    profile_form = profile_form_class(**profile_form_kwargs)
    password_form = PasswordChangeForm(request.user)
    show_password_form = False
    if request.method == "POST":
        if request.POST.get("form_type") == "profile":
            profile_form = profile_form_class(
                request.POST, **profile_form_kwargs
            )
            if profile_form.is_valid():
                profile_form.save()
                messages.success(request, "Your profile details were updated.")
                return redirect("account")
        elif request.POST.get("form_type") == "password":
            show_password_form = True
            password_form = PasswordChangeForm(request.user, request.POST)
            if password_form.is_valid():
                user = password_form.save()
                update_session_auth_hash(request, user)
                messages.success(request, "Your password was updated.")
                return redirect("account")
    orders = request.user.orders.prefetch_related("items", "tracking_events").all()
    return render(
        request,
        "home/account.html",
        {
            "orders": orders,
            "profile_form": profile_form,
            "password_form": password_form,
            "show_password_form": show_password_form,
        },
    )


@require_POST
@login_required
def cancel_order(request, order_id):
    with transaction.atomic():
        order = get_object_or_404(
            Order.objects.select_for_update().filter(user=request.user), pk=order_id
        )
        if not order.can_be_cancelled:
            messages.error(
                request,
                "This order can no longer be cancelled. Orders can be cancelled "
                "within 48 hours, before they are shipped.",
            )
            return redirect("account")

        order.status = Order.Status.CANCELLED
        order.save(update_fields=["status"])
        OrderTrackingEvent.objects.create(
            order=order,
            status=order.status,
            description="You cancelled this order.",
            updated_by=request.user,
        )

    messages.success(request, f"Order #{order.pk} has been cancelled.")
    return redirect("account")


@never_cache
@require_GET
@login_required
def order_tracking(request):
    orders = request.user.orders.prefetch_related("tracking_events")
    return JsonResponse(
        {
            "orders": [
                {
                    "id": order.pk,
                    "status": order.status,
                    "status_label": order.get_status_display(),
                    "can_cancel": order.can_be_cancelled,
                    "tracking_steps": order.tracking_steps,
                    "events": [
                        {
                            "status": event.status,
                            "status_label": event.get_status_display(),
                            "description": event.description,
                            "created_at": event.created_at.isoformat(),
                        }
                        for event in order.tracking_events.all()
                    ],
                }
                for order in orders
            ]
        }
    )


@require_POST
@login_required
def place_order(request):
    try:
        payload = json.loads(request.body)
        name = str(payload.get("name", "")).strip()
        email = str(payload.get("email", "")).strip()
        phone = str(payload.get("mobile", "")).strip()
        address = str(payload.get("address", "")).strip()
        cart = payload.get("cart")
        payment_method = payload.get("payment_method", Order.PaymentMethod.CASH_ON_DELIVERY)
        location = payload.get("location")

        if not all((name, email, phone, address)):
            return JsonResponse({"error": "Complete all delivery details."}, status=400)
        if len(name) > 160 or len(phone) > 30 or not re.fullmatch(r"[0-9+() -]{7,30}", phone):
            return JsonResponse({"error": "Enter a valid name and phone number."}, status=400)
        try:
            validate_email(email)
        except ValidationError:
            return JsonResponse({"error": "Enter a valid email address."}, status=400)
        if isinstance(location, dict):
            latitude = location.get("latitude")
            longitude = location.get("longitude")
            if (
                not isinstance(latitude, (int, float))
                or isinstance(latitude, bool)
                or not isinstance(longitude, (int, float))
                or isinstance(longitude, bool)
                or not math.isfinite(latitude)
                or not math.isfinite(longitude)
                or not -90 <= latitude <= 90
                or not -180 <= longitude <= 180
            ):
                return JsonResponse({"error": "Invalid location coordinates."}, status=400)
        elif location is None:
            latitude = longitude = None
        else:
            return JsonResponse({"error": "Invalid location coordinates."}, status=400)
        if not isinstance(cart, list) or not cart:
            return JsonResponse({"error": "Your cart is empty."}, status=400)
        if payment_method not in Order.PaymentMethod.values:
            return JsonResponse({"error": "Choose a valid payment method."}, status=400)

        quantities = {}
        for item in cart:
            if not isinstance(item, dict):
                return JsonResponse({"error": "Invalid cart item."}, status=400)
            product_id = item.get("id")
            quantity = item.get("quantity")
            if (
                not isinstance(product_id, int)
                or isinstance(product_id, bool)
                or not isinstance(quantity, int)
                or isinstance(quantity, bool)
                or quantity < 1
                or quantity > 99
            ):
                return JsonResponse({"error": "Invalid cart quantity."}, status=400)
            quantities[product_id] = quantities.get(product_id, 0) + quantity
            if quantities[product_id] > 99:
                return JsonResponse({"error": "Maximum quantity is 99 per product."}, status=400)

        with transaction.atomic():
            products = {
                product.pk: product
                for product in Product.objects.select_for_update()
                .filter(pk__in=quantities, is_active=True)
            }
            if len(products) != len(quantities):
                return JsonResponse(
                    {"error": "A product is no longer available. Refresh the store and try again."},
                    status=400,
                )
            total = sum(
                (products[pk].price * quantity for pk, quantity in quantities.items()),
                Decimal("0.00"),
            )
            order = Order.objects.create(
                user=request.user,
                full_name=name,
                email=email,
                phone=phone,
                address=address,
                payment_method=payment_method,
                total=total,
                location_latitude=latitude,
                location_longitude=longitude,
            )
            OrderItem.objects.bulk_create(
                [
                    OrderItem(
                        order=order,
                        product=products[pk],
                        product_name=products[pk].name,
                        unit_price=products[pk].price,
                        quantity=quantity,
                    )
                    for pk, quantity in quantities.items()
                ]
            )
            OrderTrackingEvent.objects.create(
                order=order,
                status=Order.Status.PENDING,
                description="We received your order. It is waiting for confirmation.",
            )
        return JsonResponse({"order_id": order.pk, "total": str(total)})
    except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
        return JsonResponse({"error": "Invalid order details."}, status=400)
